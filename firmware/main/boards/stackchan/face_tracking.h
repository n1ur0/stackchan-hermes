#pragma once
// On-device additive face tracking for the StackChan board, ported from
// Reachy Mini's HeadTracker (reachy-brain-client/body_intelligence.py).
//
// Runs a FreeRTOS task that grabs a Camera frame, converts it to RGB888,
// runs Espressif esp-dl HumanFaceDetect, and computes an ADDITIVE head
// offset (yaw/pitch in degrees) that the caller blends into the head's
// neutral pose via a callback. When no face is present the offset drifts
// back toward 0 analogously to Reachy's 0.92 decay.
//
// The class is intentionally free of esp-dl types so this header compiles
// on every board; all esp-dl code lives behind CONFIG_STACKCHAN_FACE_TRACKING
// in face_tracking.cc.

#include <cstdint>
#include <functional>
#include <memory>

class EspVideo;

class FaceTracking {
public:
    // Called from the tracking task with the additive offset in DEGREES.
    // yaw_off>0 => head turns toward camera-right; pitch_off>0 => look up.
    using WriteHeadOffsetFn = std::function<void(int yaw_off_deg, int pitch_off_deg)>;

    FaceTracking(EspVideo* camera, WriteHeadOffsetFn write_fn);
    ~FaceTracking();

    FaceTracking(const FaceTracking&) = delete;
    FaceTracking& operator=(const FaceTracking&) = delete;

    // Spawn the tracking task. Returns false if the task could not be created
    // or if this feature was compiled out. Safe to call once.
    bool Start();

    // Stop the task (joins) and reset the tracked offset to neutral.
    void Stop();

    // Enable/disable tracking on the fly. Disabling zeroes the offset and
    // stops issuing head commands (head is left where it last was).
    void SetEnabled(bool enabled);

    bool enabled() const;
    bool running() const;

private:
    struct Impl;
    std::unique_ptr<Impl> impl_;
};
