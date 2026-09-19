// On-device additive face tracking for StackChan, ported from Reachy Mini's
// HeadTracker (reachy-brain-client/body_intelligence.py).
//
// Pipeline per loop:
//   1. EspVideo::Capture() -> 320x240 (RGB565 or YUYV depending on the GC0308
//      DVP sensor's chosen pixel format).
//   2. Convert to RGB888 into a PSRAM scratch buffer.
//   3. esp-dl HumanFaceDetect::run(dl::image::img_t) on the full frame (the
//      detector's internal preprocessor resizes to the model input, MSR runs
//      single-stage detection, boxes are returned in the original frame size).
//   4. Take the FIRST face, compute normalized center, map to additive
//      yaw/pitch offsets exactly as Reachy does:
//          target_yaw   = (cx - 0.5) * 2 * MAX_YAW_TRACK
//          target_pitch = (0.5 - cy) * 2 * MAX_PITCH_TRACK   (inverted)
//      EMA-smooth: smooth += SMOOTHING * (target - smooth), SMOOTHING = 0.3.
//      No face -> smooth *= 0.92 (slow drift back toward center).
//   5. Emit the DOWNCONVERTED (int-degree) offsets via the WriteHeadAngles
//      callback so the board can add them to its neutral pose.
//
// The whole esp-dl code path is compiled out unless
// CONFIG_STACKCHAN_FACE_TRACKING=y (Kconfig opt-in).

#include "face_tracking.h"

#include "sdkconfig.h"

#include <esp_log.h>
#include <freertos/FreeRTOS.h>
#include <freertos/task.h>
#include <atomic>
#include <cmath>
#include <cstring>
#include <utility>

#if CONFIG_STACKCHAN_FACE_TRACKING
#include "esp_heap_caps.h"
#include "esp_video.h"
#include "linux/videodev2.h"  // V4L2_PIX_FMT_* constants
#include "human_face_detect.hpp"
#if CONFIG_STACKCHAN_FRAME_PUSH
#include "wifi_manager.h"  // gate frame-push on network readiness
#endif

#define FT_TAG "FaceTracking"

namespace {
// StackChan movement calibration (from m5stack reference firmware's servo
// model, m5stackchan-servo.ts): yaw travel is +/-90 deg (realistic ~+/-128),
// pitch 0..90 with M5Stack-recommended operating band 5..85 deg. The motion
// controller drives a full lookAt() vector, i.e. real tracking turns the head
// broadly toward the face -- NOT the +-7/+4.5 deg "subtle idle additive"
// offsets Reachy uses on its own head. These constants are scaled to that.
//
// MAX_YAW: a face at the frame's horizontal edge turns the head +/-45 deg so
// the movement is clearly visible. MAX_PITCH: neutral pitch is 45 deg and the
// recommended band is 5..85, so +/-20 deg keeps us comfortably inside (25..65).
// Direction: face left-of-center (cx<0.5) -> negative yaw -> head turns left
// toward it (verified live: box at left edge produced yaw=-2). NO mirror.
constexpr float kMaxYawTrackDeg   = 45.0f;   // +/-45 deg horizontal travel
constexpr float kMaxPitchTrackDeg = 20.0f;   // +/-20 deg around 45-deg neutral
constexpr float kSmoothing        = 0.4f;    // EMA factor (more responsive; ~2Hz cadence)
constexpr float kNoFaceDrift      = 0.92f;   // Reachy slow-drift-back decay
// Remote-push cadence comes from CONFIG_STACKCHAN_FRAME_PUSH_HZ when the daemon
// drives steering; fall back to the on-device detection cadence otherwise.
constexpr uint32_t kPeriodMs = 50;  // on-device detect cadence
#if CONFIG_STACKCHAN_FRAME_PUSH
constexpr uint32_t kPushPeriodMs  = 1000u / CONFIG_STACKCHAN_FRAME_PUSH_HZ;
#endif
constexpr uint32_t kTaskStackBytes = 16384;
constexpr UBaseType_t kTaskPriority = tskIDLE_PRIORITY + 2;

inline uint8_t ClampByte(float v)
{
    if (v < 0.0f) return 0;
    if (v > 255.0f) return 255;
    return (uint8_t)v;
}

// RGB565 (little-endian uint16, R=15:11, G=10:5, B=4:0) -> RGB888.
// n = number of pixels.
inline void Rgb565ToRgb888(const uint8_t* src, size_t n, uint8_t* dst)
{
    const uint16_t* s = reinterpret_cast<const uint16_t*>(src);
    for (size_t i = 0; i < n; ++i) {
        uint16_t px = s[i];
        uint8_t r = (uint8_t)((px >> 11) & 0x1F);
        r = (uint8_t)((r << 3) | (r >> 2));
        uint8_t g = (uint8_t)((px >> 5) & 0x3F);
        g = (uint8_t)((g << 2) | (g >> 4));
        uint8_t b = (uint8_t)(px & 0x1F);
        b = (uint8_t)((b << 3) | (b >> 2));
        *dst++ = r;
        *dst++ = g;
        *dst++ = b;
    }
}

// YUYV 4:2:2 (byte layout Y0 U Y1 V per 2 pixels) -> RGB888. pix = total pixels.
inline void YuyvToRgb888(const uint8_t* src, size_t pix, uint8_t* dst)
{
    for (size_t i = 0; i + 1 < pix; i += 2) {
        uint8_t cb = src[1];
        uint8_t cr = src[3];
        for (int k = 0; k < 2; ++k) {
            uint8_t Y = src[k == 0 ? 0 : 2];
            float yy = (float)Y;
            float r = yy + 1.402f * ((float)cr - 128.0f);
            float g = yy - 0.344136f * ((float)cb - 128.0f) - 0.714136f * ((float)cr - 128.0f);
            float b = yy + 1.772f * ((float)cb - 128.0f);
            *dst++ = ClampByte(r);
            *dst++ = ClampByte(g);
            *dst++ = ClampByte(b);
        }
        src += 4;
    }
}
}  // namespace

struct FaceTracking::Impl {
    EspVideo* camera_;
    FaceTracking::WriteHeadOffsetFn write_;
    TaskHandle_t task_ = nullptr;
    std::atomic<bool> running_{false};
    std::atomic<bool> enabled_{true};
    HumanFaceDetect* detect_ = nullptr;
    uint8_t* rgb_ = nullptr;    // PSRAM RGB888 scratch
    size_t rgb_cap_ = 0;
    float smooth_yaw_ = 0.0f;
    float smooth_pitch_ = 0.0f;
    // Last integer offset actually issued to the head, so we only call the
    // (bus-engaging, logging) WriteHeadAngles when the target actually changes.
    // Prevents constant servo wake / log spam while drifting on the no-face path.
    bool issued_ = false;
    int last_issued_yaw_ = 0;
    int last_issued_pitch_ = 0;

    Impl(EspVideo* c, FaceTracking::WriteHeadOffsetFn w)
        : camera_(c), write_(std::move(w))
    {
    }

    ~Impl()
    {
        ShutdownTask();
        if (detect_ != nullptr) {
            delete detect_;
            detect_ = nullptr;
        }
        if (rgb_ != nullptr) {
            heap_caps_free(rgb_);
            rgb_ = nullptr;
            rgb_cap_ = 0;
        }
    }

    // Cooperative task shutdown: signal the loop to exit, then wait for the
    // TaskMain trampoline to clear task_ just before it self-deletes. We must
    // NOT vTaskDelete() from the outside (could kill the task holding a mutex).
    void ShutdownTask()
    {
        if (!running_.load(std::memory_order_acquire)) {
            smooth_yaw_ = 0.0f;
            smooth_pitch_ = 0.0f;
            return;
        }
        running_.store(false, std::memory_order_release);
        const TickType_t deadline = xTaskGetTickCount() + pdMS_TO_TICKS(500);
        while (task_ != nullptr && xTaskGetTickCount() < deadline) {
            vTaskDelay(pdMS_TO_TICKS(10));
        }
        smooth_yaw_ = 0.0f;
        smooth_pitch_ = 0.0f;
    }

    static void TaskMain(void* arg)
    {
        auto* impl = static_cast<Impl*>(arg);
        impl->Loop();
        impl->running_.store(false, std::memory_order_release);
        impl->task_ = nullptr;
        vTaskDelete(nullptr);
    }

    bool EnsureRgbBuffer(uint32_t w, uint32_t h)
    {
        size_t need = (size_t)w * (size_t)h * 3u;
        if (rgb_ != nullptr && rgb_cap_ >= need) return true;
        if (rgb_ != nullptr) {
            heap_caps_free(rgb_);
            rgb_ = nullptr;
            rgb_cap_ = 0;
        }
        rgb_ = (uint8_t*)heap_caps_malloc(need, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
        if (rgb_ == nullptr) {
            ESP_LOGE(FT_TAG, "alloc RGB888 buffer failed: %u bytes", (unsigned)need);
            return false;
        }
        rgb_cap_ = need;
        return true;
    }

    void RunDetect(uint32_t w, uint32_t h)
    {
        dl::image::img_t img = {rgb_, (uint16_t)w, (uint16_t)h,
                                dl::image::DL_IMAGE_PIX_TYPE_RGB888};
        auto& results = detect_->run(img);
        if (!results.empty()) {
            // FIRST face only, matching Reachy (results.front()).
            const dl::detect::result_t& r = results.front();
            float x1 = (float)r.box[0];
            float y1 = (float)r.box[1];
            float x2 = (float)r.box[2];
            float y2 = (float)r.box[3];
            float bw = x2 - x1;
            float bh = y2 - y1;
            float cx = (x1 + bw * 0.5f) / (float)w;
            float cy = (y1 + bh * 0.5f) / (float)h;

            float target_yaw = (cx - 0.5f) * 2.0f * kMaxYawTrackDeg;
            float target_pitch = (0.5f - cy) * 2.0f * kMaxPitchTrackDeg;  // inverted

            smooth_yaw_ += kSmoothing * (target_yaw - smooth_yaw_);
            smooth_pitch_ += kSmoothing * (target_pitch - smooth_pitch_);

            MaybeIssue(smooth_yaw_, smooth_pitch_);

            ESP_LOGD(FT_TAG,
                     "face score=%.2f box=(%.0f,%.0f,%.0f,%.0f) off=(yaw=%d,pitch=%d)",
                     r.score, x1, y1, x2, y2,
                     (int)lroundf(smooth_yaw_), (int)lroundf(smooth_pitch_));
        } else {
            // No face: slow drift back toward neutral.
            smooth_yaw_ *= kNoFaceDrift;
            smooth_pitch_ *= kNoFaceDrift;
            MaybeIssue(smooth_yaw_, smooth_pitch_);
            ESP_LOGD(FT_TAG, "no face, drifting off=(yaw=%d,pitch=%d)",
                     (int)lroundf(smooth_yaw_), (int)lroundf(smooth_pitch_));
        }
    }

    // Issue a head command only when the integer offset changes from the last
    // issued one by more than a small deadband (or on first issue). Prevents
    // constant servo wake / log spam while the EMA on a still face jitters
    // around an integer boundary, or while drifting back to neutral.
    static constexpr int kIssueDeadbandDeg = 1;
    void MaybeIssue(float yaw, float pitch)
    {
        int yo = (int)lroundf(yaw);
        int po = (int)lroundf(pitch);
        if (issued_
            && std::abs(yo - last_issued_yaw_) <= kIssueDeadbandDeg
            && std::abs(po - last_issued_pitch_) <= kIssueDeadbandDeg) {
            return;
        }
        issued_ = true;
        last_issued_yaw_ = yo;
        last_issued_pitch_ = po;
        if (write_) write_(yo, po);
    }

    void Loop()
    {
        // Remote-push mode: when a dedicated tracking URL is configured, don't
        // run the heavy on-device esp-dl detector at all — just capture + JPEG
        // + POST frames at kPushPeriodMs and let the nerv0x YuNet daemon steer
        // the head via move_head. This is the primary head-tracking path.
#if CONFIG_STACKCHAN_FRAME_PUSH && defined(CONFIG_STACKCHAN_TRACKING_URL) \
    && !defined(CONFIG_STACKCHAN_TRACKING_URL_FORCE_OFF)
        const bool remote_push = (CONFIG_STACKCHAN_TRACKING_URL[0] != '\0');
#else
        const bool remote_push = false;
#endif

        if (!remote_push) {
            detect_ = new HumanFaceDetect();  // on-device fallback (MSRMNP_S8_V1)
            if (detect_ == nullptr) {
                ESP_LOGE(FT_TAG, "failed to create HumanFaceDetect");
                return;
            }
        }
        ESP_LOGI(FT_TAG, "face tracking task started (remote=%d)", (int)remote_push);

        while (running_.load(std::memory_order_acquire)) {
            if (!enabled_.load(std::memory_order_acquire)) {
                // Disabled: idle the offset back to center, issue no commands.
                smooth_yaw_ = 0.0f;
                smooth_pitch_ = 0.0f;
#if CONFIG_STACKCHAN_FRAME_PUSH
                vTaskDelay(pdMS_TO_TICKS(kPushPeriodMs));
#else
                vTaskDelay(pdMS_TO_TICKS(kPeriodMs));
#endif
                continue;
            }

            if (remote_push) {
                // Only push once the network is up — the HTTP POST path
                // asserts/reboots if lwIP isn't ready (observed: "Invalid mbox"
                // in tcpip_send_msg_wait_sem). Wait idle until connected.
#if CONFIG_STACKCHAN_FRAME_PUSH
                if (!WifiManager::GetInstance().IsConnected()) {
                    vTaskDelay(pdMS_TO_TICKS(kPushPeriodMs));
                    continue;
                }
                camera_->PushFrameForTracking(60);
                vTaskDelay(pdMS_TO_TICKS(kPushPeriodMs));
#else
                vTaskDelay(pdMS_TO_TICKS(kPeriodMs));
#endif
                continue;
            }

            if (camera_ != nullptr && camera_->Capture()) {
                uint8_t* frame = nullptr;
                size_t len = 0;
                uint32_t fmt = 0;
                uint16_t w = 0, h = 0;
                if (camera_->GetLastFrame(&frame, &len, &fmt, &w, &h) && frame != nullptr && w > 0 && h > 0) {
                    if (EnsureRgbBuffer(w, h) && rgb_ != nullptr) {
                        size_t n = (size_t)w * (size_t)h;
                        if (fmt == V4L2_PIX_FMT_RGB565) {
                            Rgb565ToRgb888(frame, n, rgb_);
                            RunDetect(w, h);
                        } else if (fmt == V4L2_PIX_FMT_RGB24) {
                            memcpy(rgb_, frame, n * 3u);
                            RunDetect(w, h);
                        } else if (fmt == V4L2_PIX_FMT_YUYV) {
                            YuyvToRgb888(frame, n, rgb_);
                            RunDetect(w, h);
                        } else {
                            ESP_LOGW(FT_TAG, "unsupported camera format 0x%lX", fmt);
                        }
                    }
                }
            } else {
                ESP_LOGD(FT_TAG, "Capture failed / camera null");
            }
            vTaskDelay(pdMS_TO_TICKS(kPeriodMs));
        }
    }
};

bool FaceTracking::Start()
{
    if (impl_ == nullptr || impl_->running_.load(std::memory_order_acquire)) {
        return false;
    }
    impl_->smooth_yaw_ = 0.0f;
    impl_->smooth_pitch_ = 0.0f;
    impl_->enabled_.store(true, std::memory_order_release);
    impl_->running_.store(true, std::memory_order_release);
    BaseType_t ok = xTaskCreate(&FaceTracking::Impl::TaskMain, "face_track",
                                kTaskStackBytes, impl_.get(), kTaskPriority, &impl_->task_);
    if (ok != pdPASS) {
        impl_->running_.store(false, std::memory_order_release);
        ESP_LOGE(FT_TAG, "xTaskCreate failed");
        return false;
    }
    return true;
}

void FaceTracking::Stop()
{
    if (impl_ == nullptr) return;
    impl_->ShutdownTask();
}

void FaceTracking::SetEnabled(bool enabled)
{
    if (impl_) impl_->enabled_.store(enabled, std::memory_order_release);
}

bool FaceTracking::enabled() const
{
    return impl_ ? impl_->enabled_.load(std::memory_order_acquire) : false;
}

bool FaceTracking::running() const
{
    return impl_ ? impl_->running_.load(std::memory_order_acquire) : false;
}

FaceTracking::FaceTracking(EspVideo* camera, FaceTracking::WriteHeadOffsetFn write_fn)
    : impl_(new Impl(camera, std::move(write_fn)))
{
}

FaceTracking::~FaceTracking() = default;

#else  // !CONFIG_STACKCHAN_FACE_TRACKING — compiled-out, no-op stubs.

bool FaceTracking::Start() { return false; }
void FaceTracking::Stop() {}
void FaceTracking::SetEnabled(bool) {}
bool FaceTracking::enabled() const { return false; }
bool FaceTracking::running() const { return false; }

FaceTracking::FaceTracking(EspVideo*, FaceTracking::WriteHeadOffsetFn) {}
FaceTracking::~FaceTracking() = default;

#endif  // CONFIG_STACKCHAN_FACE_TRACKING
