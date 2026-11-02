import AVFoundation
import CoreImage
import Foundation

struct CameraFailure: LocalizedError {
    let message: String
    var errorDescription: String? { message }
}

func emit(_ event: [String: Any], image: Data? = nil) throws {
    var data = try JSONSerialization.data(withJSONObject: event)
    data.append(0x0a)
    if let image { data.append(image) }
    try FileHandle.standardOutput.write(contentsOf: data)
}

func authorize() throws {
    if AVCaptureDevice.authorizationStatus(for: .video) == .notDetermined {
        let answered = DispatchSemaphore(value: 0)
        AVCaptureDevice.requestAccess(for: .video) { _ in answered.signal() }
        guard answered.wait(timeout: .now() + 90) == .success else {
            throw CameraFailure(message: "Camera permission is waiting. Allow camera access, then try again.")
        }
    }
    guard AVCaptureDevice.authorizationStatus(for: .video) == .authorized else {
        throw CameraFailure(message: "Allow camera access in System Settings → Privacy & Security → Camera, then try again.")
    }
}

func cameras() -> [AVCaptureDevice] {
    AVCaptureDevice.DiscoverySession(
        deviceTypes: [.builtInWideAngleCamera, .external, .continuityCamera],
        mediaType: .video, position: .unspecified
    ).devices
}

final class Camera: NSObject, AVCaptureVideoDataOutputSampleBufferDelegate {
    let session = AVCaptureSession()
    let output = AVCaptureVideoDataOutput()
    let frames = DispatchQueue(label: "app.waitless.camera.frames")
    let condition = NSCondition()
    let context = CIContext()
    var requestedAfter: CMTime?
    var pendingFrame: CVPixelBuffer?
    var failure: String?
    var observers: [NSObjectProtocol] = []
    var frameNumber = 0

    func open(_ device: AVCaptureDevice) throws {
        session.beginConfiguration()
        defer { session.commitConfiguration() }
        let input = try AVCaptureDeviceInput(device: device)
        guard session.canAddInput(input), session.canAddOutput(output) else {
            throw CameraFailure(message: "This camera cannot provide a video input.")
        }
        session.addInput(input)
        session.addOutput(output)
        // Native stream stills retain the chosen input's highest session quality.
        if session.canSetSessionPreset(.high) { session.sessionPreset = .high }
        output.alwaysDiscardsLateVideoFrames = true
        output.videoSettings = [kCVPixelBufferPixelFormatTypeKey as String: kCVPixelFormatType_32BGRA]
        output.setSampleBufferDelegate(self, queue: frames)
        if let connection = output.connection(with: .video), connection.isVideoMirroringSupported {
            connection.automaticallyAdjustsVideoMirroring = false
            connection.isVideoMirrored = false
        }
        observers.append(NotificationCenter.default.addObserver(
            forName: .AVCaptureSessionRuntimeError, object: session, queue: nil
        ) { [weak self] note in
            self?.fail((note.userInfo?[AVCaptureSessionErrorKey] as? Error)?.localizedDescription ?? "Camera input failed.")
        })
        observers.append(NotificationCenter.default.addObserver(
            forName: .AVCaptureDeviceWasDisconnected, object: device, queue: nil
        ) { [weak self] _ in self?.fail("The selected camera disconnected. Reconnect it and refresh the preview.") })
    }

    func fail(_ message: String) {
        condition.lock()
        failure = message
        condition.signal()
        condition.unlock()
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
        guard let buffer else {
            throw CameraFailure(message: "No fresh frame from the selected camera. Check that it is connected and not paused or in use by another app.")
        }
        let image = CIImage(cvPixelBuffer: buffer)
        guard let jpeg = context.jpegRepresentation(of: image, colorSpace: CGColorSpaceCreateDeviceRGB(),
            options: [kCGImageDestinationLossyCompressionQuality as CIImageRepresentationOption: 0.95]) else {
            throw CameraFailure(message: "Could not encode the camera frame.")
        }
        frameNumber += 1
        return jpeg
    }

    func close() {
        session.stopRunning()
        output.setSampleBufferDelegate(nil, queue: nil)
        observers.forEach { NotificationCenter.default.removeObserver($0) }
    }
}

func run() throws {
    try authorize()
    let args = CommandLine.arguments
    if args.count == 2 && args[1] == "--list" {
        try emit(["event": "devices", "devices": cameras().map {
            ["id": $0.uniqueID, "name": $0.localizedName, "kind": $0.isContinuityCamera ? "iphone" : "camera"]
        }])
        return
    }
    guard args.count == 3, args[1] == "--device",
          let device = cameras().first(where: { $0.uniqueID == args[2] }) else {
        throw CameraFailure(message: "The selected camera is unavailable. Refresh the camera list and choose an input.")
    }
    let camera = Camera()
    try camera.open(device)
    defer { camera.close() }
    camera.session.startRunning()
    guard camera.session.isRunning else { throw CameraFailure(message: "Could not start the selected camera.") }
    try emit(["event": "ready", "model": device.localizedName, "device_id": device.uniqueID])
    while let line = readLine() {
        let parts = line.split(separator: " ")
        if line == "QUIT" { return }
        guard parts.count == 2, parts[0] == "FRAME", let timeout = Double(parts[1]),
              timeout.isFinite, timeout > 0, timeout <= 20 else {
            throw CameraFailure(message: "Invalid camera request.")
        }
        let data = try camera.frame(timeout: timeout)
        try emit(["event": "frame", "bytes": data.count, "frame_no": camera.frameNumber], image: data)
    }
}

DispatchQueue.global(qos: .userInitiated).async {
    do {
        try run()
        exit(0)
    } catch {
        try? emit(["event": "error", "message": error.localizedDescription])
        exit(1)
    }
}
RunLoop.main.run()
