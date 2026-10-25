// Offline policy tests: no SDK library, camera connection, or shutter calls.
#include "camera_capabilities.h"
#include <iostream>

using namespace futurelens;
using namespace SCRSDK;

void expect(bool condition, const char* message) {
    if (!condition) throw std::runtime_error(message);
}

Snapshot camera() {
    Snapshot result;
    result.shutter_release = true;
    result.properties = {
        {CrDeviceProperty_LiveViewStatus, {true, false, CrLiveView_Enable, {}}},
        {CrDeviceProperty_ExposureProgramMode, {true, false, CrExposure_M_Manual, {}}},
        {CrDeviceProperty_DriveMode, {true, true, CrDrive_Single, {CrDrive_Single}}},
        {CrDeviceProperty_StillImageStoreDestination, {true, true, CrStillImageStoreDestination_MemoryCard,
            {CrStillImageStoreDestination_HostPC, CrStillImageStoreDestination_HostPCAndMemoryCard, CrStillImageStoreDestination_MemoryCard}}},
        {CrDeviceProperty_Still_Image_Trans_Size, {true, true, CrPropertyStillImageTransSize_SmallSize,
            {CrPropertyStillImageTransSize_Original, CrPropertyStillImageTransSize_SmallSize}}},
        {CrDeviceProperty_FileType, {true, false, CrFileType_Jpeg, {}}},
        {CrDeviceProperty_FocusMode, {true, false, CrFocus_AF_S, {}}},
        {CrDeviceProperty_S1, {true, true, CrLockIndicator_Unlocked, {CrLockIndicator_Locked, CrLockIndicator_Unlocked}}},
        {CrDeviceProperty_FocusIndication, {true, false, CrFocusIndicator_Unlocked, {}}},
    };
    return result;
}

int main() {
    expect(select_usb({{"ILCE-1", "IP"}, {"ILCE-6700", "USB"}}) == 1, "USB camera selection must not filter by model");
    expect(select_usb({{"Future Sony Model", "USB"}}) == 0, "No fixed model whitelist");
    for (const auto& devices : std::vector<std::vector<Device>>{
             {}, {{"ILCE-1", "IP"}}, {{"ILCE-6700", "USB"}, {"ILCE-7RM5", "USB"}},
             {{"ILCE-6700", "USB"}, {"ILCE-6700", "USB"}}}) {
        bool rejected = false;
        try { select_usb(devices); } catch (const std::runtime_error&) { rejected = true; }
        expect(rejected, "Missing or ambiguous USB cameras must fail");
    }

    uint16_t values[] = {1, 256, 513};
    uint8_t unaligned[sizeof(values) + 1];
    std::memcpy(unaligned + 1, values, sizeof(values));
    expect(enum_values(CrDataType_UInt16Array, unaligned + 1, sizeof(values)) == std::vector<uint64_t>({1, 256, 513}), "Decode unaligned enum buffers");
    expect(enum_values(CrDataType_UInt16Array, values, 3).empty(), "Reject incomplete enum buffers");
    expect(enum_values(CrDataType_UInt16Range, values, sizeof(values)).empty(), "Ranges are not value lists");
    expect(enum_values(CrDataType_UInt16Array, nullptr, 2).empty(), "Missing value buffers are not supported values");
    expect(enum_values(CrDataType_Button, values, sizeof(values)).size() == 3, "Decode command button values");

    auto state = camera();
    auto caps = assess(state);
    expect(caps.capture_ready() && caps.autofocus && caps.expected_files == 1, "AF camera should be capture-ready");
    expect(caps.destination == CrStillImageStoreDestination_HostPCAndMemoryCard, "Prefer saving to both computer and card");

    for (auto code : {CrDeviceProperty_LiveViewStatus, CrDeviceProperty_DriveMode, CrDeviceProperty_ExposureProgramMode,
                     CrDeviceProperty_StillImageStoreDestination, CrDeviceProperty_FileType,
                     CrDeviceProperty_FocusMode, CrDeviceProperty_S1, CrDeviceProperty_FocusIndication}) {
        state = camera();
        state.properties.erase(code);
        expect(!assess(state).capture_ready(), "Reject absent required properties before capture");
    }
    state = camera();
    state.shutter_release = false;
    expect(!assess(state).capture_ready() && assess(state).live_view, "Preview-only camera must not acquire shutter permission");
    state = camera();
    state.properties[CrDeviceProperty_LiveViewStatus].current = CrLiveView_Disable;
    expect(!assess(state).capture_ready() && !assess(state).live_view, "Temporarily disabled preview must fail");
    state = camera();
    state.properties[CrDeviceProperty_DriveMode].current = 0xffff;
    expect(!assess(state).capture_ready(), "Never enable burst or unknown drive modes");
    for (auto mode : {CrExposure_P_Auto, CrExposure_A_AperturePriority, CrExposure_S_ShutterSpeedPriority,
                      CrExposure_M_Manual, CrExposure_Auto, CrExposure_STILL}) {
        state = camera();
        state.properties[CrDeviceProperty_ExposureProgramMode].current = mode;
        expect(assess(state).capture_ready(), "Support photo exposure modes across camera families");
    }
    for (auto mode : {CrExposure_Movie_M, CrExposure_MOVIE, CrExposure_Movie_SQMotion_AUTO,
                      CrExposure_HiFrameRate_P, CrExposure_SweepPanorama, CrExposure_Movie_IntervalRec_F}) {
        state = camera();
        state.properties[CrDeviceProperty_ExposureProgramMode].current = mode;
        expect(!assess(state).capture_ready(), "Single drive alone must not allow movie or panorama capture");
    }

    state = camera();
    state.properties[CrDeviceProperty_StillImageStoreDestination].values = {CrStillImageStoreDestination_HostPC};
    expect(assess(state).capture_ready() && assess(state).destination == CrStillImageStoreDestination_HostPC, "Support computer-only storage");
    state.properties[CrDeviceProperty_StillImageStoreDestination].writable = false;
    expect(!assess(state).capture_ready(), "Do not set a locked transfer destination");
    state.properties[CrDeviceProperty_StillImageStoreDestination].current = CrStillImageStoreDestination_HostPC;
    expect(assess(state).capture_ready(), "An already-correct read-only destination needs no change");

    state = camera();
    state.properties.erase(CrDeviceProperty_Still_Image_Trans_Size);
    expect(assess(state).capture_ready() && !assess(state).adjustable_transfer_size, "Support cameras without an adjustable transfer size");
    state = camera();
    state.properties[CrDeviceProperty_Still_Image_Trans_Size].values.clear();
    expect(!assess(state).capture_ready(), "Writability alone does not prove Original is a supported value");
    state.properties[CrDeviceProperty_Still_Image_Trans_Size].current = CrPropertyStillImageTransSize_Original;
    state.properties[CrDeviceProperty_Still_Image_Trans_Size].writable = false;
    expect(assess(state).capture_ready(), "Keep an already-original read-only transfer size");

    state = camera();
    state.properties[CrDeviceProperty_FocusMode].current = CrFocus_MF;
    state.properties.erase(CrDeviceProperty_S1);
    state.properties.erase(CrDeviceProperty_FocusIndication);
    expect(assess(state).capture_ready() && !assess(state).autofocus, "MF must not require AF controls");
    state.properties[CrDeviceProperty_FocusMode].current = 0xffff;
    expect(!assess(state).capture_ready(), "Unknown focus mode must fail closed");
    state = camera();
    state.properties[CrDeviceProperty_S1].current = CrLockIndicator_Locked;
    expect(!assess(state).capture_ready(), "Do not take over an already-half-pressed camera");
    state = camera();
    state.properties[CrDeviceProperty_S1].values = {CrLockIndicator_Locked};
    expect(!assess(state).capture_ready(), "Both half-press and release must be supported");
    state = camera();
    state.properties[CrDeviceProperty_S1].readable = false;
    expect(assess(state).capture_ready(), "A set-only half-press can use readable focus confirmation");

    for (bool jpeg : {true, false}) {
        state = camera();
        state.properties[CrDeviceProperty_FileType].current = jpeg ? CrFileType_RawJpeg : CrFileType_RawHeif;
        expect(!assess(state).capture_ready(), "Paired formats need a known transfer selection");
        auto& save = state.properties[CrDeviceProperty_RAW_J_PC_Save_Image];
        save.readable = true;
        save.current = jpeg ? CrPropertyRAWJPCSaveImage_RAWAndJPEG : CrPropertyRAWJPCSaveImage_RAWAndHEIF;
        expect(assess(state).expected_files == 2, "Wait for both files in paired transfers");
        save.current = CrPropertyRAWJPCSaveImage_RAWOnly;
        expect(assess(state).expected_files == 1, "RAW-only transfer has one file");
        save.current = jpeg ? CrPropertyRAWJPCSaveImage_HEIFOnly : CrPropertyRAWJPCSaveImage_JPEGOnly;
        expect(!assess(state).capture_ready(), "Reject inconsistent file/transfer formats");
    }
    std::cout << "USB discovery and camera capability policy checks passed\n";
}
