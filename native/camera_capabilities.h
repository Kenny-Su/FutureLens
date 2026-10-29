#pragma once

#include <algorithm>
#include <cstdint>
#include <cstring>
#include <map>
#include <optional>
#include <stdexcept>
#include <string>
#include <vector>

#include "CrDeviceProperty.h"
#include "CrControlCode.h"

namespace waitless {
namespace sdk = SCRSDK;

struct Device {
    std::string model;
    std::string connection;
};

inline size_t select_usb(const std::vector<Device>& devices) {
    std::vector<size_t> matches;
    for (size_t i = 0; i < devices.size(); ++i)
        if (devices[i].connection == "USB") matches.push_back(i);
    if (matches.empty())
        throw std::runtime_error("No supported Sony USB camera found. Select Remote Shooting / PC Remote on the camera and allow USB access.");
    if (matches.size() > 1) {
        std::string message = "Multiple Sony USB cameras detected:";
        for (auto i : matches) message += " [" + std::to_string(i + 1) + "] " + devices[i].model;
        throw std::runtime_error(message + ". Connect only one camera before starting.");
    }
    return matches.front();
}

// Only unsigned enumerations are used by this workflow. Never treat a range or
// malformed byte buffer as a list of supported setting values.
inline std::vector<uint64_t> enum_values(sdk::CrDataType type, const void* data, size_t bytes) {
    if (!(type & sdk::CrDataType_ArrayBit) || (type & sdk::CrDataType_RangeBit) ||
        (type & sdk::CrDataType_SignBit) || !data) return {};
    const auto scalar = type & 0xF;
    if (scalar < sdk::CrDataType_UInt8 || scalar > sdk::CrDataType_UInt64) return {};
    const size_t width = size_t(1) << (scalar - 1);
    if (bytes % width) return {};
    std::vector<uint64_t> result;
    for (size_t offset = 0; offset < bytes; offset += width) {
        uint64_t value = 0;
        std::memcpy(&value, static_cast<const uint8_t*>(data) + offset, width);
        result.push_back(value);
    }
    return result;
}

struct Property {
    bool readable = false;
    bool writable = false;
    uint64_t current = 0;
    std::vector<uint64_t> values;

    bool can_set(uint64_t value) const {
        return writable && std::find(values.begin(), values.end(), value) != values.end();
    }
    bool can_use(uint64_t value) const {
        return readable && (current == value || can_set(value));
    }
};

struct Snapshot {
    std::map<CrInt32u, Property> properties;
    bool shutter_release = false;
    std::string command_error;
    const Property& get(CrInt32u code) const {
        static const Property missing;
        auto found = properties.find(code);
        return found == properties.end() ? missing : found->second;
    }
};

struct Capabilities {
    bool live_view = false;
    bool autofocus = false;
    bool adjustable_transfer_size = false;
    std::optional<uint64_t> destination;
    size_t expected_files = 0;
    std::vector<std::string> preview_issues;
    std::vector<std::string> capture_issues;
    bool capture_ready() const { return live_view && capture_issues.empty(); }
};

inline bool still_exposure(uint64_t mode) {
    using namespace sdk;
    return (mode >= CrExposure_M_Manual && mode <= CrExposure_Portrait) ||
           (mode >= CrExposure_Auto && mode <= CrExposure_HighSensitivity) ||
           mode == CrExposure_MemoryRecall || mode == CrExposure_Flash_Off ||
           mode == CrExposure_PictureEffect || mode == CrExposure_STILL;
}

inline Capabilities assess(const Snapshot& state) {
    using namespace sdk;
    Capabilities result;
    const auto& live = state.get(CrDeviceProperty_LiveViewStatus);
    result.live_view = live.readable && live.current == CrLiveView_Enable;
    if (!result.live_view)
        result.preview_issues.push_back("Live view is unsupported or currently disabled. Close camera menus/playback and check Remote Shooting mode.");

    if (!state.shutter_release)
        result.capture_issues.push_back(state.command_error.empty()
            ? "Still-photo shutter release is not advertised by this camera."
            : state.command_error);
    const auto& exposure = state.get(CrDeviceProperty_ExposureProgramMode);
    if (!exposure.readable || !still_exposure(exposure.current))
        result.capture_issues.push_back("A supported still-photo shooting mode is not selected. Select Still or a photo P/A/S/M mode.");
    const auto& drive = state.get(CrDeviceProperty_DriveMode);
    if (!drive.readable || drive.current != CrDrive_Single)
        result.capture_issues.push_back("Single Shooting is unavailable or not selected. Use still-photo mode and Single Shooting drive mode.");

    const auto& destination = state.get(CrDeviceProperty_StillImageStoreDestination);
    if (destination.can_use(CrStillImageStoreDestination_HostPCAndMemoryCard))
        result.destination = CrStillImageStoreDestination_HostPCAndMemoryCard;
    else if (destination.can_use(CrStillImageStoreDestination_HostPC))
        result.destination = CrStillImageStoreDestination_HostPC;
    else
        result.capture_issues.push_back("Saving captured photos to the computer is unavailable or locked by camera settings.");

    result.adjustable_transfer_size = state.properties.count(CrDeviceProperty_Still_Image_Trans_Size) != 0;
    if (result.adjustable_transfer_size &&
        !state.get(CrDeviceProperty_Still_Image_Trans_Size).can_use(CrPropertyStillImageTransSize_Original))
        result.capture_issues.push_back("Original-size photo transfer is unavailable or locked. Select Original on the camera.");

    const auto& file = state.get(CrDeviceProperty_FileType);
    if (file.readable && (file.current == CrFileType_Jpeg || file.current == CrFileType_Raw || file.current == CrFileType_Heif))
        result.expected_files = 1;
    else if (file.readable && (file.current == CrFileType_RawJpeg || file.current == CrFileType_RawHeif)) {
        const auto& save = state.get(CrDeviceProperty_RAW_J_PC_Save_Image);
        const bool jpeg = file.current == CrFileType_RawJpeg;
        if (save.readable) {
            if (save.current == (jpeg ? CrPropertyRAWJPCSaveImage_RAWAndJPEG : CrPropertyRAWJPCSaveImage_RAWAndHEIF))
                result.expected_files = 2;
            else if (save.current == CrPropertyRAWJPCSaveImage_RAWOnly ||
                     save.current == (jpeg ? CrPropertyRAWJPCSaveImage_JPEGOnly : CrPropertyRAWJPCSaveImage_HEIFOnly))
                result.expected_files = 1;
        }
    }
    if (!result.expected_files)
        result.capture_issues.push_back("Cannot determine the photo file type or RAW/JPEG/HEIF computer-transfer selection.");

    const auto& focus = state.get(CrDeviceProperty_FocusMode);
    if (!focus.readable)
        result.capture_issues.push_back("The camera does not expose its current focus mode.");
    else if (focus.current == CrFocus_MF || focus.current == CrFocus_PF) {
        result.autofocus = false;
    } else if (focus.current == CrFocus_AF_S || focus.current == CrFocus_AF_C ||
               focus.current == CrFocus_AF_A || focus.current == CrFocus_AF_D || focus.current == CrFocus_DMF) {
        result.autofocus = true;
        const auto& half = state.get(CrDeviceProperty_S1);
        if (!half.can_set(CrLockIndicator_Locked) || !half.can_set(CrLockIndicator_Unlocked))
            result.capture_issues.push_back("Remote half-press is unavailable in this focus mode. Switch the camera to MF.");
        else if (half.readable && half.current != CrLockIndicator_Unlocked)
            result.capture_issues.push_back("Release the camera's half-pressed shutter before starting.");
        if (!state.get(CrDeviceProperty_FocusIndication).readable)
            result.capture_issues.push_back("Autofocus confirmation is unavailable. Switch the camera to MF.");
    } else {
        result.capture_issues.push_back("Unrecognized focus mode. Switch the camera to MF.");
    }
    return result;
}
} // namespace waitless
