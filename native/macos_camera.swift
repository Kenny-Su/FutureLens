import AVFoundation
import CoreImage
import Foundation

struct CameraFailure: LocalizedError {
    let message: String
    var errorDescription: String? { message }
}

let outputLock = NSLock()
func emit(_ event: [String: Any], image: Data? = nil) throws {
    var data = try JSONSerialization.data(withJSONObject: event)
    data.append(0x0a)
    if let image { data.append(image) }
    outputLock.lock()
    defer { outputLock.unlock() }
    try FileHandle.standardOutput.write(contentsOf: data)
}

func authorize() throws {
    if AVCaptureDevice.authorizationStatus(for: .video) == .notDetermined {
        let answered = DispatchSemaphore(value: 0)
        AVCaptureDevice.requestAccess(for: .video) { _ in answered.signal() }
        guard answered.wait(timeout: .now() + 90) == .success else {
            throw CameraFailure(message: "Allow WaitLess Camera to access your camera, then try again.")
        }
    }
    guard AVCaptureDevice.authorizationStatus(for: .video) == .authorized else {
        throw CameraFailure(message: "Allow WaitLess Camera in System Settings → Privacy & Security → Camera, then try again.")
    }
}

func cameras() -> [AVCaptureDevice] {
    let discovery = AVCaptureDevice.DiscoverySession(deviceTypes: [.continuityCamera], mediaType: .video,
                                                     position: .unspecified)
    let deadline = Date(timeIntervalSinceNow: 3)
    while discovery.devices.isEmpty && Date() < deadline { Thread.sleep(forTimeInterval: 0.1) }
    return discovery.devices.filter { $0.isContinuityCamera }
}

final class Camera: NSObject, AVCaptureVideoDataOutputSampleBufferDelegate, AVCapturePhotoCaptureDelegate {
    let session = AVCaptureSession()
    let videoOutput = AVCaptureVideoDataOutput()
    let photoOutput = AVCapturePhotoOutput()
    let frames = DispatchQueue(label: "app.waitless.camera.frames")
    let condition = NSCondition()
    let context = CIContext()
    var rotation: AVCaptureDevice.RotationCoordinator?
    var requestedAfter: CMTime?
    var pendingFrame: CVPixelBuffer?
    var failure: String?
    var observers: [NSObjectProtocol] = []
    var frameNumber = 0
    var photoID: Int64?
    var photoData: Data?
    var photoError: String?
    var photoComplete = false

    func open(_ device: AVCaptureDevice) throws {
        guard device.isContinuityCamera else { throw CameraFailure(message: "Only iPhone Continuity Camera is supported.") }
        session.beginConfiguration()
        defer { session.commitConfiguration() }
        let input = try AVCaptureDeviceInput(device: device)
        guard session.canAddInput(input) else { throw CameraFailure(message: "The iPhone camera is unavailable.") }
        session.addInput(input)
        if session.canSetSessionPreset(.photo) { session.sessionPreset = .photo }
        guard session.canAddOutput(videoOutput), session.canAddOutput(photoOutput) else {
            throw CameraFailure(message: "This iPhone cannot provide previews and still photos together.")
        }
        session.addOutput(videoOutput)
        session.addOutput(photoOutput)
        photoOutput.maxPhotoQualityPrioritization = .quality
        // Apple's Continuity Camera opt-in (WWDC22/10018). Setting maxPhotoDimensions
        // restricted stills to the advertised video sizes on the tested macOS/iPhone pair.
        photoOutput.isHighResolutionCaptureEnabled = true
        guard photoOutput.availablePhotoCodecTypes.contains(.jpeg) else {
            throw CameraFailure(message: "The iPhone does not support processed JPEG photo capture.")
        }
        videoOutput.alwaysDiscardsLateVideoFrames = true
        videoOutput.videoSettings = [kCVPixelBufferPixelFormatTypeKey as String: kCVPixelFormatType_32BGRA]
        videoOutput.setSampleBufferDelegate(self, queue: frames)
        rotation = AVCaptureDevice.RotationCoordinator(device: device, previewLayer: nil)
        for output in [videoOutput as AVCaptureOutput, photoOutput as AVCaptureOutput] {
            if let connection = output.connection(with: .video), connection.isVideoMirroringSupported {
                connection.automaticallyAdjustsVideoMirroring = false
                connection.isVideoMirrored = false
            }
        }
        observers.append(NotificationCenter.default.addObserver(forName: .AVCaptureSessionRuntimeError,
            object: session, queue: nil) { [weak self] note in
            self?.fail((note.userInfo?[AVCaptureSessionErrorKey] as? Error)?.localizedDescription ?? "The iPhone camera failed.")
        })
        observers.append(NotificationCenter.default.addObserver(forName: .AVCaptureDeviceWasDisconnected,
            object: device, queue: nil) { [weak self] _ in self?.fail("iPhone disconnected. Reconnect it and review the scene again.") })
    }

    func orient(_ output: AVCaptureOutput) {
        if let connection = output.connection(with: .video), let rotation {
            let angle = rotation.videoRotationAngleForHorizonLevelCapture
            if connection.isVideoRotationAngleSupported(angle) { connection.videoRotationAngle = angle }
        }
    }

    func fail(_ message: String) {
        condition.lock()
        let first = failure == nil
        failure = message
        condition.broadcast()
        condition.unlock()
        if first { try? emit(["event": "error", "message": message]) }
    }

    func captureOutput(_ output: AVCaptureOutput, didOutput sample: CMSampleBuffer, from connection: AVCaptureConnection) {
        condition.lock()
        defer { condition.unlock() }
        guard let after = requestedAfter,
              CMTimeCompare(CMSampleBufferGetPresentationTimeStamp(sample), after) >= 0,
              let buffer = CMSampleBufferGetImageBuffer(sample) else { return }
        pendingFrame = buffer
        requestedAfter = nil
        condition.signal()
    }

    func frame(timeout: Double) throws -> Data {
        orient(videoOutput)
        condition.lock()
        pendingFrame = nil
        requestedAfter = CMClockGetTime(CMClockGetHostTimeClock())
        let deadline = Date(timeIntervalSinceNow: timeout)
        while pendingFrame == nil && failure == nil {
            if !condition.wait(until: deadline) { break }
        }
        let buffer = pendingFrame
        let error = failure
        pendingFrame = nil
        requestedAfter = nil
        condition.unlock()
        if let error { throw CameraFailure(message: error) }
        guard let buffer else { throw CameraFailure(message: "No fresh iPhone preview. Check that Continuity Camera is connected and not paused.") }
        let image = CIImage(cvPixelBuffer: buffer)
        guard let jpeg = context.jpegRepresentation(of: image, colorSpace: CGColorSpaceCreateDeviceRGB(),
            options: [kCGImageDestinationLossyCompressionQuality as CIImageRepresentationOption: 0.9]) else {
            throw CameraFailure(message: "Could not encode the iPhone preview.")
        }
        frameNumber += 1
        return jpeg
    }

    func photo(timeout: Double) throws -> Data {
        let deadline = Date(timeIntervalSinceNow: timeout)
        orient(photoOutput)
        let settings = AVCapturePhotoSettings(format: [AVVideoCodecKey: AVVideoCodecType.jpeg])
        settings.isHighResolutionPhotoEnabled = true
        settings.photoQualityPrioritization = .quality
        settings.flashMode = .off
        condition.lock()
        let error = failure
        photoID = settings.uniqueID
        photoData = nil
        photoError = nil
        photoComplete = false
        condition.unlock()
        if let error { throw CameraFailure(message: error) }
        guard session.isRunning && Date() < deadline else { throw CameraFailure(message: "The photo request expired before capture.") }
        try emit(["event": "shutter"])
        photoOutput.capturePhoto(with: settings, delegate: self)
        // Once triggered, allow Apple's photo processing to finish even if the preview expires.
        condition.lock()
        let processingDeadline = Date(timeIntervalSinceNow: 90)
        while !photoComplete && failure == nil {
            if !condition.wait(until: processingDeadline) { break }
        }
        let data = photoData
        let captureError = failure ?? photoError
        let complete = photoComplete
        photoID = nil
        condition.unlock()
        if let captureError { throw CameraFailure(message: captureError) }
        guard complete, let data else { throw CameraFailure(message: "The iPhone did not finish processing the photo. Capture is unconfirmed; no automatic retry.") }
        return data
    }

    func photoOutput(_ output: AVCapturePhotoOutput, didFinishProcessingPhoto photo: AVCapturePhoto, error: Error?) {
        condition.lock()
        defer { condition.unlock() }
        guard photo.resolvedSettings.uniqueID == photoID else { return }
        photoError = error?.localizedDescription
        if error == nil { photoData = photo.fileDataRepresentation() }
    }

    func photoOutput(_ output: AVCapturePhotoOutput, didFinishCaptureFor resolvedSettings: AVCaptureResolvedPhotoSettings, error: Error?) {
        condition.lock()
        defer { condition.unlock() }
        guard resolvedSettings.uniqueID == photoID else { return }
        if let error { photoError = error.localizedDescription }
        photoComplete = true
        condition.signal()
    }

    func close() {
        observers.forEach { NotificationCenter.default.removeObserver($0) }
        session.stopRunning()
        videoOutput.setSampleBufferDelegate(nil, queue: nil)
    }
}

func run() throws {
    try authorize()
    let args = CommandLine.arguments
    if args.count == 2 && args[1] == "--list" {
        try emit(["event": "devices", "devices": cameras().map {
            ["id": $0.uniqueID, "name": $0.localizedName, "kind": "iphone"]
        }])
        return
    }
    guard (args.count == 3 || (args.count == 4 && args[3] == "--capture")), args[1] == "--device",
          let device = cameras().first(where: { $0.uniqueID == args[2] }) else {
        throw CameraFailure(message: "No matching iPhone Continuity Camera. Bring your iPhone nearby or connect it by USB, then find it again.")
    }
    let allowCapture = args.count == 4
    let camera = Camera()
    try camera.open(device)
    defer { camera.close() }
    camera.session.startRunning()
    guard camera.session.isRunning else { throw CameraFailure(message: "Could not start iPhone Continuity Camera.") }
    _ = try camera.frame(timeout: 15)
    try emit(["event": "ready", "model": device.localizedName, "device_id": device.uniqueID,
              "capture_kind": "photo", "high_resolution": camera.photoOutput.isHighResolutionCaptureEnabled,
              "quality_prioritization": "quality"])
    while let line = readLine() {
        if line == "QUIT" { return }
        let parts = line.split(separator: " ")
        guard parts.count == 2, let timeout = Double(parts[1]), timeout.isFinite, timeout > 0, timeout <= 20 else {
            throw CameraFailure(message: "Invalid camera request.")
        }
        if parts[0] == "FRAME" {
            let data = try camera.frame(timeout: timeout)
            try emit(["event": "frame", "bytes": data.count, "frame_no": camera.frameNumber], image: data)
        } else if parts[0] == "PHOTO" && allowCapture {
            let data = try camera.photo(timeout: timeout)
            try emit(["event": "photo", "bytes": data.count], image: data)
        } else { throw CameraFailure(message: "Still-photo capture is disabled in preview mode.") }
    }
}

DispatchQueue.global(qos: .userInitiated).async {
    do { try run(); exit(0) }
    catch { try? emit(["event": "error", "message": error.localizedDescription]); exit(1) }
}
RunLoop.main.run()
