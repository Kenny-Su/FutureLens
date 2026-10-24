// A persistent, single-camera Sony SDK bridge. SDK calls stay on the main thread;
// callbacks only update state. stdout is reserved for JSON lines + JPEG payloads.
#include <chrono>
#include <algorithm>
#include <condition_variable>
#include <cmath>
#include <filesystem>
#include <iomanip>
#include <iostream>
#include <mutex>
#include <sstream>
#include <stdexcept>
#include <string>
#include <thread>
#include <vector>

#include "CameraRemote_SDK.h"
#include "IDeviceCallback.h"

namespace sdk = SCRSDK;
namespace fs = std::filesystem;
using namespace std::chrono_literals;

std::string quote(const std::string& value) {
    std::ostringstream out;
    out << '"';
    for (unsigned char c : value) {
        if (c == '"' || c == '\\') out << '\\' << c;
        else if (c < 32) out << "\\u" << std::hex << std::setw(4) << std::setfill('0') << unsigned(c);
        else out << c;
    }
    out << '"';
    return out.str();
}

void check(sdk::CrError code, const char* action) {
    if (code) {
        std::ostringstream message;
        message << action << " failed (Sony SDK 0x" << std::hex << code << ")";
        throw std::runtime_error(message.str());
    }
}

class Camera : public sdk::IDeviceCallback {
    std::mutex mutex;
    std::condition_variable changed;
    bool initialized = false, connected = false;
    bool shutter_down = false, half_pressed = false;
    CrInt32u error = 0;
    sdk::CrDeviceHandle handle = 0;
    sdk::ICrEnumCameraObjectInfo* cameras = nullptr;
    std::vector<std::string> downloads;
    std::vector<std::pair<CrInt32u, CrInt64u>> restore;

    void healthy() {
        std::lock_guard<std::mutex> lock(mutex);
        if (!connected) throw std::runtime_error("Camera disconnected");
        check(error, "Camera callback");
    }

    sdk::CrDeviceProperty property(CrInt32u code) {
        sdk::CrDeviceProperty* properties = nullptr;
        CrInt32 count = 0;
        check(sdk::GetSelectDeviceProperties(handle, 1, &code, &properties, &count), "Read property");
        if (!properties || count != 1) {
            if (properties) sdk::ReleaseDeviceProperties(handle, properties);
            throw std::runtime_error("Camera property unavailable: " + std::to_string(code));
        }
        sdk::CrDeviceProperty result = properties[0];
        sdk::ReleaseDeviceProperties(handle, properties);
        return result;
    }

    void set_property(CrInt32u code, CrInt64u value, bool remember = true) {
        auto prop = property(code);
        if (prop.GetCurrentValue() == value) return;
        if (!prop.IsSetEnableCurrentValue())
            throw std::runtime_error("Camera setting is not writable: " + std::to_string(code));
        // Remember before sending: a timeout may still leave the setting changed.
        if (remember) restore.emplace_back(code, prop.GetCurrentValue());
        prop.SetCurrentValue(value);
        check(sdk::SetDeviceProperty(handle, &prop), "Set property");
        for (int i = 0; i < 30; ++i) {
            std::this_thread::sleep_for(100ms);
            healthy();
            if (property(code).GetCurrentValue() == value) return;
        }
        throw std::runtime_error("Camera setting change timed out");
    }

public:
    std::string model;

    void OnConnected(sdk::DeviceConnectionVersioin) override {
        std::lock_guard<std::mutex> lock(mutex);
        connected = true;
        changed.notify_all();
    }
    void OnDisconnected(CrInt32u code) override {
        std::lock_guard<std::mutex> lock(mutex);
        connected = false;
        error = code;
        changed.notify_all();
    }
    void OnError(CrInt32u code) override {
        std::lock_guard<std::mutex> lock(mutex);
        error = code;
        changed.notify_all();
    }
    void OnCompleteDownload(CrChar* filename, CrInt32u type) override {
        std::lock_guard<std::mutex> lock(mutex);
        if (filename && type == 0xFFFFFFFF && std::find(downloads.begin(), downloads.end(), filename) == downloads.end()) {
            downloads.emplace_back(filename);
            std::cerr << "Photo downloaded: " << filename << '\n';
        }
        changed.notify_all();
    }
    void OnWarning(CrInt32u code) override {
        std::lock_guard<std::mutex> lock(mutex);
        std::cerr << "Sony notification: 0x" << std::hex << code << std::dec << '\n';
        changed.notify_all();
    }

    void connect() {
        initialized = sdk::Init();
        if (!initialized) throw std::runtime_error("Sony SDK initialization failed");
        check(sdk::EnumCameraObjects(&cameras, 3), "Enumerate cameras");
        sdk::ICrCameraObjectInfo* selected = nullptr;
        if (cameras) for (CrInt32u i = 0; i < cameras->GetCount(); ++i) {
            auto* info = const_cast<sdk::ICrCameraObjectInfo*>(cameras->GetCameraObjectInfo(i));
            if (std::string(info->GetConnectionTypeName()) != "USB") continue;
            if (std::string(info->GetModel()) != "ILCE-7M4") continue;
            if (selected) throw std::runtime_error("Connect only one ILCE-7M4 for this prototype");
            selected = info;
        }
        if (!selected) throw std::runtime_error("No USB ILCE-7M4 found. Select Remote Shooting on the camera and allow USB access.");
        model = selected->GetModel();
        check(sdk::Connect(selected, this, &handle, sdk::CrSdkControlMode_Remote,
                           sdk::CrReconnecting_OFF), "Connect");
        std::unique_lock<std::mutex> lock(mutex);
        if (!changed.wait_for(lock, 15s, [&] { return connected || error; }))
            throw std::runtime_error("Camera connection timed out");
        check(error, "Connect callback");
        lock.unlock();
        std::this_thread::sleep_for(1s);
    }

    void frame() {
        healthy();
        sdk::CrImageInfo info;
        check(sdk::GetLiveViewImageInfo(handle, &info), "Live-view info");
        auto size = info.GetBufferSize();
        if (!size || size > 32 * 1024 * 1024) throw std::runtime_error("Invalid live-view buffer size");
        std::vector<CrInt8u> buffer(size);
        sdk::CrImageDataBlock data;
        data.SetData(buffer.data());
        data.SetSize(size);
        check(sdk::GetLiveViewImage(handle, &data), "Live-view image");
        if (!data.GetImageData() || !data.GetImageSize() || data.GetImageSize() > size)
            throw std::runtime_error("Empty live-view image");
        std::cout << "{\"event\":\"frame\",\"frame_no\":" << data.GetFrameNo()
                  << ",\"bytes\":" << data.GetImageSize() << "}\n";
        std::cout.write(reinterpret_cast<const char*>(data.GetImageData()), data.GetImageSize());
        std::cout.flush();
    }

    void prepare_capture() {
        healthy();
        if (property(sdk::CrDeviceProperty_DriveMode).GetCurrentValue() != sdk::CrDrive_Single)
            throw std::runtime_error("Set the camera Drive Mode to Single Shooting before capture");
        set_property(sdk::CrDeviceProperty_StillImageStoreDestination,
                     sdk::CrStillImageStoreDestination_HostPCAndMemoryCard);
        set_property(sdk::CrDeviceProperty_Still_Image_Trans_Size,
                     sdk::CrPropertyStillImageTransSize_Original);
        std::cerr << "Capture ready. Focus mode: " << property(sdk::CrDeviceProperty_FocusMode).GetCurrentValue() << '\n';
    }

    void capture(const fs::path& folder, double max_delay) {
        const auto expires = std::chrono::steady_clock::now() + std::chrono::duration<double>(max_delay);
        healthy();
        // Recheck in case the user changed drive mode while the model was working.
        if (property(sdk::CrDeviceProperty_DriveMode).GetCurrentValue() != sdk::CrDrive_Single)
            throw std::runtime_error("Drive Mode changed; capture cancelled");
        auto file_type = property(sdk::CrDeviceProperty_FileType).GetCurrentValue();
        size_t expected_files = 1;
        if (file_type == sdk::CrFileType_RawJpeg || file_type == sdk::CrFileType_RawHeif) {
            auto pc_save = property(sdk::CrDeviceProperty_RAW_J_PC_Save_Image).GetCurrentValue();
            if (pc_save == sdk::CrPropertyRAWJPCSaveImage_RAWAndJPEG || pc_save == sdk::CrPropertyRAWJPCSaveImage_RAWAndHEIF)
                expected_files = 2;
            else if (pc_save != sdk::CrPropertyRAWJPCSaveImage_JPEGOnly && pc_save != sdk::CrPropertyRAWJPCSaveImage_RAWOnly
                     && pc_save != sdk::CrPropertyRAWJPCSaveImage_HEIFOnly)
                throw std::runtime_error("Unrecognized PC image-save setting");
        } else if (file_type != sdk::CrFileType_Jpeg && file_type != sdk::CrFileType_Raw && file_type != sdk::CrFileType_Heif)
            throw std::runtime_error("Unrecognized still-image file type");
        if (!fs::create_directory(folder)) throw std::runtime_error("Capture directory already exists");
        std::string path = folder.string(), prefix = "FL_";
        check(sdk::SetSaveInfo(handle, path.data(), prefix.data(), 1), "Set capture destination");
        // Follow Sony's afShooting sample: half press, await focus, then full press.
        auto focus_mode = property(sdk::CrDeviceProperty_FocusMode).GetCurrentValue();
        half_pressed = true;
        set_property(sdk::CrDeviceProperty_S1, sdk::CrLockIndicator_Locked, false);
        if (focus_mode != sdk::CrFocus_MF) {
            bool focused = false;
            for (int i = 0; i < 30; ++i) {
                healthy();
                auto focus = property(sdk::CrDeviceProperty_FocusIndication).GetCurrentValue();
                if (focus == sdk::CrFocusIndicator_Focused_AF_S || focus == sdk::CrFocusIndicator_Focused_AF_C
                    || focus == sdk::CrFocusIndicator_TrackingSubject_AF_C) { focused = true; break; }
                if (std::chrono::steady_clock::now() >= expires) break;
                std::this_thread::sleep_for(100ms);
            }
            if (!focused) throw std::runtime_error("Autofocus did not lock in time; no shutter command sent. Use MF or adjust focus.");
        }
        if (std::chrono::steady_clock::now() >= expires)
            throw std::runtime_error("Decision expired during camera preparation; no shutter command sent");
        {
            std::lock_guard<std::mutex> lock(mutex);
            downloads.clear();
        }
        shutter_down = true;
        std::cerr << "Sending shutter down\n";
        check(sdk::SendCommand(handle, sdk::CrCommandId_Release, sdk::CrCommandParam_Down), "Shutter down");
        std::this_thread::sleep_for(35ms);
        check(sdk::SendCommand(handle, sdk::CrCommandId_Release, sdk::CrCommandParam_Up), "Shutter up");
        shutter_down = false;
        std::cout << "{\"event\":\"shutter_sent\"}" << std::endl;
        set_property(sdk::CrDeviceProperty_S1, sdk::CrLockIndicator_Unlocked, false);
        half_pressed = false;
        std::cerr << "Shutter released; waiting for " << expected_files << " photo file(s)\n";
        std::unique_lock<std::mutex> lock(mutex);
        if (!changed.wait_for(lock, 60s, [&] {
            return downloads.size() >= expected_files || !connected || error;
        })) throw std::runtime_error("Photo transfer timed out; shutter may have fired. No automatic retry.");
        if (!connected) throw std::runtime_error("Camera disconnected during capture; no automatic retry");
        check(error, "Capture callback");
        std::cout << "{\"event\":\"captured\",\"files\":[";
        for (size_t i = 0; i < downloads.size(); ++i) {
            if (i) std::cout << ',';
            fs::path file(downloads[i]);
            if (!file.is_absolute()) file = folder / file;
            std::cout << quote(file.string());
        }
        std::cout << "]}" << std::endl;
    }

    void close() {
        if (handle) {
            if (shutter_down) {
                auto code = sdk::SendCommand(handle, sdk::CrCommandId_Release, sdk::CrCommandParam_Up);
                if (code) std::cerr << "Could not release shutter: " << code << '\n';
                shutter_down = false;
            }
            if (half_pressed) {
                try { set_property(sdk::CrDeviceProperty_S1, sdk::CrLockIndicator_Unlocked, false); }
                catch (const std::exception& e) { std::cerr << "Half-press release: " << e.what() << '\n'; }
                half_pressed = false;
            }
            for (auto it = restore.rbegin(); it != restore.rend(); ++it) {
                try { healthy(); set_property(it->first, it->second, false); }
                catch (const std::exception& e) { std::cerr << "Camera setting restore: " << e.what() << '\n'; }
            }
            restore.clear();
            sdk::Disconnect(handle);
            {
                std::unique_lock<std::mutex> lock(mutex);
                changed.wait_for(lock, 3s, [&] { return !connected; });
            }
            sdk::ReleaseDevice(handle);
            handle = 0;
        }
        if (cameras) { cameras->Release(); cameras = nullptr; }
        if (initialized) { sdk::Release(); initialized = false; }
    }
    ~Camera() { close(); }
};

int main(int argc, char** argv) {
    try {
        if (argc < 2 || argc > 3) throw std::runtime_error("Usage: sony_camera OUTPUT_DIRECTORY [--enable-capture]");
        fs::path output = fs::absolute(argv[1]);
        bool capture_enabled = argc == 3 && std::string(argv[2]) == "--enable-capture";
        if (argc == 3 && !capture_enabled) throw std::runtime_error("Unknown option");
        if (!fs::is_directory(output)) throw std::runtime_error("Output directory must exist");
        Camera camera;
        camera.connect();
        std::cout << "{\"event\":\"ready\",\"model\":" << quote(camera.model) << "}" << std::endl;
        bool prepared = false;
        std::string command;
        while (std::getline(std::cin, command)) {
            if (command == "QUIT") break;
            if (command == "FRAME") camera.frame();
            else if (command == "PREPARE" && capture_enabled) {
                if (!prepared) { camera.prepare_capture(); prepared = true; }
                std::cout << "{\"event\":\"prepared\"}" << std::endl;
            }
            else if (command.rfind("CAPTURE ", 0) == 0 && capture_enabled) {
                std::istringstream args(command.substr(8));
                std::string id, extra;
                double max_delay = 0;
                args >> id >> max_delay;
                if (id.empty() || id.size() > 16 || id.find_first_not_of("0123456789") != std::string::npos)
                    throw std::runtime_error("Invalid capture ID");
                if (!std::isfinite(max_delay) || max_delay <= 0 || (args >> extra))
                    throw std::runtime_error("Invalid capture expiry");
                if (!prepared) throw std::runtime_error("Prepare the camera before capture");
                camera.capture(output / id, max_delay);
            } else throw std::runtime_error("Unknown or disabled command");
        }
        camera.close();
        std::cout << "{\"event\":\"closed\"}" << std::endl;
        return 0;
    } catch (const std::exception& e) {
        std::cout << "{\"event\":\"error\",\"message\":" << quote(e.what()) << "}" << std::endl;
        return 1;
    }
}
