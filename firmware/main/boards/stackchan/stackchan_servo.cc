// StackChan board — generated split of the former stackchan.cc god-file.
// Zero behavior change; see stackchan_board.h for the class declaration.

#include "stackchan_board.h"

#include "wifi_board.h"
#include "cores3_audio_codec.h"
#include "display/lcd_display.h"
#include "application.h"
#include "config.h"
#include "power_save_timer.h"
#include "i2c_device.h"
#include "axp2101.h"
#include "mcp_server.h"
#include "settings.h"
#include "led_strip.h"
// Issue #79: servo driver is selectable at build time via Kconfig.
//   - CONFIG_STACKCHAN_SERVO_SCSCL  (default): GPL-3.0 SCServo_lib
//   - CONFIG_STACKCHAN_SERVO_FEETECH: MIT clean-room driver vendored at
//     firmware/components/feetech_scs/.
// Both drivers share the same begin / WritePos / ReadPos call signatures
#include "avatar_images.h"
#include "avatar_set.h"
#include "avatar_set_fetcher.h"

#include <smooth_ui_toolkit.hpp>
#include <esp_log.h>
#include <driver/i2c_master.h>
#include <driver/gpio.h>
#include <driver/uart.h>
#include <esp_lcd_panel_io.h>
#include <esp_lcd_panel_ops.h>
#include <esp_lcd_ili9341.h>
#include <esp_timer.h>
#include <esp_random.h>
#include <freertos/FreeRTOS.h>
#include <freertos/task.h>
#include <freertos/semphr.h>
#include "esp_video.h"
#include <cJSON.h>
#include <lvgl.h>
#include <algorithm>
#include <atomic>
#include <cmath>
#include <limits>
#include <memory>
#include <string>
#include <vector>

int StackChanBoard::YawDegToPos(int deg) {

    int pos = 460 + deg * 16 / 5;
    if (pos < 0) pos = 0;
    if (pos > 1000) pos = 1000;
    return pos;

}

int StackChanBoard::PitchDegToPos(int deg) {

    // Issue #80: defense-in-depth — clamp at the servo-write boundary so
    // motion-task interpolation and any other future caller cannot
    // bypass the input-layer clamp.
    if (deg < SAFE_PITCH_MIN) deg = SAFE_PITCH_MIN;
    if (deg > SAFE_PITCH_MAX) deg = SAFE_PITCH_MAX;
    int pos = 620 + deg * 16 / 5;
    if (pos < 0) pos = 0;
    if (pos > 1000) pos = 1000;
    return pos;

}

uint16_t StackChanBoard::clamp_u16(uint32_t v) {

    if (v > std::numeric_limits<uint16_t>::max()) {
        return std::numeric_limits<uint16_t>::max();
    }
    return static_cast<uint16_t>(v);

}

smooth_ui_toolkit::SpringOptions_t StackChanBoard::MapDurationToSpringOptions(uint32_t duration_ms) {

    if (duration_ms == 0) {
        duration_ms = 1;
    }

    float speed_f = 500.0f *
        (static_cast<float>(MOTION_DEFAULT_DURATION_MS) /
         static_cast<float>(duration_ms));
    if (speed_f < 1.0f) speed_f = 1.0f;
    if (speed_f > 1000.0f) speed_f = 1000.0f;
    int speed = static_cast<int>(speed_f);

    constexpr float kMin = 10.0f;
    constexpr float kMax = 650.0f;
    constexpr float kMass = 1.0f;
    float normalized_speed = static_cast<float>(speed) / 1000.0f;
    float stiffness =
        kMin + (normalized_speed * normalized_speed) * (kMax - kMin);
    float damping = 2.0f * std::sqrt(kMass * stiffness);

    smooth_ui_toolkit::SpringOptions_t options;
    options.stiffness = stiffness;
    options.damping = damping;
    options.mass = kMass;
    options.velocity = 0.0f;
    options.restDelta = speed > 800 ? 0.5f : 0.1f;
    options.restSpeed = speed > 800 ? 0.5f : 0.1f;
    options.duration = 0.0f;
    options.bounce = 0.0f;
    options.visualDuration = 0.0f;
    return options;

}

void StackChanBoard::InitializeServo() {

    ESP_LOGI(TAG, "Init SCS0009 servo bus (UART%d, baud=%d, tx=%d, rx=%d)",
             SERVO_UART_NUM, SERVO_BAUDRATE, SERVO_TX_PIN, SERVO_RX_PIN);

    // Resolve the neutral (rest) pose from NVS before any boot-init seed
    // below references it. Persisted by self.robot.set_neutral_pose
    // (namespace "stackchan_pose"); the BOOT_INIT_* constants are the
    // first-boot defaults. Mirrors the proximity-config NVS load in
    // InitializeLtr553Proximity(). pitch is clamped on write, but clamp
    // again here as defense-in-depth in case the stored value predates a
    // SAFE_PITCH range change.
    {
        Settings settings("stackchan_pose");
        neutral_yaw_   = settings.GetInt("yaw", BOOT_INIT_YAW_DEG);
        neutral_pitch_ = settings.GetInt("pitch", BOOT_INIT_PITCH_DEG);
        if (neutral_yaw_ < -90) neutral_yaw_ = -90;
        if (neutral_yaw_ > 90)  neutral_yaw_ = 90;
        if (neutral_pitch_ < SAFE_PITCH_MIN) neutral_pitch_ = SAFE_PITCH_MIN;
        if (neutral_pitch_ > SAFE_PITCH_MAX) neutral_pitch_ = SAFE_PITCH_MAX;
    }
    ESP_LOGI(TAG, "neutral pose config: yaw=%d pitch=%d", neutral_yaw_, neutral_pitch_);
#if CONFIG_STACKCHAN_SERVO_FEETECH
    // FeetechScs::begin() returns void and uses ESP_ERROR_CHECK internally,
    // so a UART configuration error aborts the boot rather than reporting
    // false. If begin() returns to us, init succeeded.
    scs_bus_.begin(SERVO_UART_NUM, SERVO_BAUDRATE, SERVO_TX_PIN, SERVO_RX_PIN);
    servo_ok_ = true;
#else
    // SCServo_lib SCSCL::begin() returns bool — false if UART setup failed.
    servo_ok_ = scs_bus_.begin(SERVO_UART_NUM, SERVO_BAUDRATE, SERVO_TX_PIN, SERVO_RX_PIN);
#endif
    // ACK reading is enabled (SCS::Level defaults to 1). genWrite() will
    // wait for the SCS0009's 6-byte ACK packet before returning, which
    // implicitly enforces an inter-frame gap and prevents a follow-up
    // WritePos from colliding with a still-processing servo. This aligns
    // with the M5 StackChan official BSP behaviour (which never touches
    // Level). Was: scs_bus_.Level = 0 — turned out to silently drop
    // every WritePos after the first one ("starts moving once, then
    // never again" symptom).
    ESP_LOGI(TAG, "Servo bus init: %s (Level=1, ACK enabled)", servo_ok_ ? "OK" : "FAILED");

    if (servo_ok_) {
        motion_mutex_ = xSemaphoreCreateMutex();
        scs_bus_mutex_ = xSemaphoreCreateMutex();
        if (motion_mutex_ == nullptr || scs_bus_mutex_ == nullptr) {
            ESP_LOGE(TAG, "Failed to create servo mutexes: motion=%p scs_bus=%p; disabling servo",
                     motion_mutex_, scs_bus_mutex_);
            if (motion_mutex_ != nullptr) {
                vSemaphoreDelete(motion_mutex_);
                motion_mutex_ = nullptr;
            }
            if (scs_bus_mutex_ != nullptr) {
                vSemaphoreDelete(scs_bus_mutex_);
                scs_bus_mutex_ = nullptr;
            }
            servo_ok_ = false;
            return;
        }

#if CONFIG_STACKCHAN_SERVO_DELEGATED_MOTION
        motion_driver_ = std::make_unique<ServoDelegatedMotionDriver>(
            scs_bus_, scs_bus_mutex_, motion_mutex_,
            yaw_motion_, pitch_motion_);
#else
        motion_driver_ = std::make_unique<HostInterpolationMotionDriver>(
            scs_bus_, scs_bus_mutex_, motion_mutex_,
            yaw_motion_, pitch_motion_);
#endif
        if (!motion_driver_->Initialize()) {
            ESP_LOGE(TAG, "Failed to initialize motion driver; disabling servo");
            motion_driver_.reset();
            vSemaphoreDelete(motion_mutex_);
            motion_mutex_ = nullptr;
            vSemaphoreDelete(scs_bus_mutex_);
            scs_bus_mutex_ = nullptr;
            servo_ok_ = false;
            return;
        }

        // Issue #121 (Problem 1, "downward drop on power-on") + #123
        // (boot-init diagnostics).
        //
        // Background: the SCS0009 retains its commanded set-point
        // across power cycles (Hypothesis 1 in #121, confirmed by
        // the firmware-v1.4.1 clean-install reproduction -- after a
        // full NVS reset on the ESP32 side, the boot pre-init
        // `ReadPos` still matched the pre-power-off pose exactly,
        // demonstrating the set-point lives in the servo itself,
        // not in firmware-side NVS). When VM_EN asserts at boot,
        // the servo restores torque and snaps toward that retained
        // target before any firmware-side speed limiting can apply
        // -- audible as a mechanical end-stop impact when the
        // previous session ended near pitch=0, and visible as a
        // downward drop in general.
        //
        // The mitigation is a `WritePos(id, current_pos, time=0,
        // speed=0)` per servo, which the SCS0009 treats as a new
        // target equal to its current position. This interrupts any
        // in-progress snap motion and leaves the servo stationary
        // at the raw position the immediately-preceding `ReadPos`
        // observed, until the subsequent interpolating boot-init
        // climb begins.
        //
        // Efficacy depends on the `ReadPos` + `WritePos` pair
        // completing while the servo is still mid-snap. To minimize
        // that window, pitch (the only axis that exhibits the
        // downward drop) is read AND held BEFORE the yaw axis is
        // touched on the SCS0009 bus -- any yaw `ReadPos` /
        // `WritePos` / ACK wait interposed between the pitch
        // `ReadPos` and pitch `WritePos` would widen the window
        // and risk the snap completing into an end-stop before the
        // pitch hold reaches the servo. The unified "Boot pre-init
        // ReadPos" diagnostic line (#123) is emitted after both
        // holds because it is purely informational and not on the
        // timing-critical path. If a `ReadPos` value lands close
        // to a previous session's commanded target (e.g. raw pitch
        // near 620 for pitch=0 deg), the snap was likely still in
        // progress and the hold is expected to truncate it; if a
        // `ReadPos` value is already at an end-stop or fails, the
        // hold for that boot is a no-op or skipped and a deeper
        // fix (e.g. firmware-controlled VM_EN sequencing through
        // the PY32 IO-expander) would be required, tracked
        // separately.

        // Phase 1a: pitch first -- read and immediately hold.
        //
        // Issue #138: retry ReadPos to absorb the SCS0009 ~200 ms
        // startup latency observed after VM_EN HIGH on the PMIC
        // long-press OFF/ON path. Without retry, the first ReadPos
        // (measured at around tick 140 on this hardware) typically
        // lands inside the wake-up window and returns -1, causing
        // the snap-suppress hold below to skip on exactly the path
        // #121 Problem 1 targets. Budget: 5 attempts × 50 ms = 250 ms
        // total per axis, well above the observed ~200 ms latency.
        // This is distinct from the SCS0009 bus hang (#100), which a
        // fixed retry budget cannot clear; in that case all attempts
        // fail and the safe-fallback branch in Phase 2 below seeds
        // pitch_motion_.current_deg with BOOT_INIT_PITCH_DEG to avoid
        // an end-stop walk during the subsequent boot-init
        // `WriteHeadAngles` interpolation.
        // Loop form mirrors the `get_head_angles` MCP-tool retry below
        // (set attempts to `i + 1` inside the loop so the final value
        // equals the number of attempts actually made, even when all
        // retries fail). The previous `for (attempts = 1; attempts <=
        // MAX; ++attempts)` form left attempts at MAX+1 on failure
        // and made the diagnostic log overstate the attempt count.
        constexpr int BOOT_READPOS_MAX_ATTEMPTS = 5;
        constexpr uint32_t BOOT_READPOS_RETRY_MS = 50;
        int pitch_pos_actual = -1;
        int pitch_attempts = 0;
        for (int i = 0; i < BOOT_READPOS_MAX_ATTEMPTS; ++i) {
            pitch_attempts = i + 1;
            pitch_pos_actual = scs_bus_.ReadPos(SERVO_PITCH_ID);
            if (pitch_pos_actual >= 0) break;
            if (i + 1 < BOOT_READPOS_MAX_ATTEMPTS) {
                vTaskDelay(pdMS_TO_TICKS(BOOT_READPOS_RETRY_MS));
            }
        }
        if (pitch_pos_actual >= 0) {
            // Bound the snap-suppress hold to the SAFE_PITCH_MIN..
            // SAFE_PITCH_MAX range applied at every other pitch
            // servo-write boundary in this file (see PitchDegToPos
            // and the Phase 2 restored_pitch clamp below). If the
            // boot `ReadPos` lands outside that range -- for example
            // because the servo bus came back online holding a
            // previous session's out-of-range set-point, or the
            // head was hand-pushed beyond an end-stop -- writing
            // the raw position back would bypass that safety
            // boundary and pin the servo against the stall current
            // it is held there from. In that case skip the hold
            // and let the subsequent interpolating boot-init climb
            // to (yaw=0, pitch=45) drive the head back into the
            // safe range through the existing speed-limited path.
            constexpr int PITCH_SAFE_RAW_MIN =
                620 + SAFE_PITCH_MIN * 16 / 5;  // raw 620 at deg=0
            constexpr int PITCH_SAFE_RAW_MAX =
                620 + SAFE_PITCH_MAX * 16 / 5;  // raw 901 at deg=88
            if (pitch_pos_actual >= PITCH_SAFE_RAW_MIN &&
                pitch_pos_actual <= PITCH_SAFE_RAW_MAX) {
                int pitch_hold_r = scs_bus_.WritePos(
                    SERVO_PITCH_ID, pitch_pos_actual, 0, 0);
                ESP_LOGI(TAG,
                         "Boot snap-suppress pitch hold(pos=%d): r=%d",
                         pitch_pos_actual, pitch_hold_r);
            } else {
                ESP_LOGW(TAG,
                         "Boot snap-suppress pitch skipped: ReadPos=%d outside safe raw range [%d, %d]; relying on boot-init climb",
                         pitch_pos_actual,
                         PITCH_SAFE_RAW_MIN, PITCH_SAFE_RAW_MAX);
            }
        }

        // Phase 1b: yaw second -- no analogous snap-into-end-stop
        // failure mode, so timing is not critical. Retry budget
        // matches pitch (Issue #138) for symmetry; in practice yaw
        // typically succeeds on the first attempt because the pitch
        // retries above have already consumed the SCS0009 startup-
        // latency window on the shared bus.
        int yaw_pos_actual = -1;
        int yaw_attempts = 0;
        for (int i = 0; i < BOOT_READPOS_MAX_ATTEMPTS; ++i) {
            yaw_attempts = i + 1;
            yaw_pos_actual = scs_bus_.ReadPos(SERVO_YAW_ID);
            if (yaw_pos_actual >= 0) break;
            if (i + 1 < BOOT_READPOS_MAX_ATTEMPTS) {
                vTaskDelay(pdMS_TO_TICKS(BOOT_READPOS_RETRY_MS));
            }
        }
        if (yaw_pos_actual >= 0) {
            int yaw_hold_r = scs_bus_.WritePos(
                SERVO_YAW_ID, yaw_pos_actual, 0, 0);
            ESP_LOGI(TAG,
                     "Boot snap-suppress yaw hold(pos=%d): r=%d",
                     yaw_pos_actual, yaw_hold_r);
        }

        // Phase 1c (diagnostic, #123): unified pre-init ReadPos log
        // with tick timestamp. Off the timing-critical path
        // intentionally; ServoTask has not been created yet, so no
        // `scs_bus_mutex_` contention is possible at this point.
        ESP_LOGI(TAG,
                 "Boot pre-init ReadPos: yaw_raw=%d (attempts=%d) "
                 "pitch_raw=%d (attempts=%d) tick=%u",
                 yaw_pos_actual, yaw_attempts,
                 pitch_pos_actual, pitch_attempts,
                 (unsigned)xTaskGetTickCount());

        // Phase 2: software-side current_deg restore. Order does not
        // affect the SCS0009 bus -- these only update firmware-side
        // motion state for the upcoming interpolating boot-init
        // climb.
        if (yaw_pos_actual >= 0) {
            yaw_motion_.current_deg = (yaw_pos_actual - 460) * 5 / 16;
            ESP_LOGI(TAG, "Restored yaw_motion_.current_deg=%d from ReadPos=%d",
                     yaw_motion_.current_deg, yaw_pos_actual);
        } else {
            // Issue #138: seed yaw current_deg to the neutral pose
            // rather than leaving the struct default. neutral_yaw_ is the
            // NVS-resolved rest pose (default BOOT_INIT_YAW_DEG); seeding
            // to it keeps the boot-init climb a near-no-op
            // (start_deg == target_deg) so the start==target invariant
            // holds regardless of the persisted neutral. The explicit
            // form keeps intent visible.
            yaw_motion_.current_deg = neutral_yaw_;
            ESP_LOGW(TAG,
                     "Failed to ReadPos(yaw) after %d attempts; "
                     "seeded current_deg=%d (neutral_yaw_)",
                     BOOT_READPOS_MAX_ATTEMPTS, neutral_yaw_);
        }
        if (pitch_pos_actual >= 0) {
            int restored_pitch = (pitch_pos_actual - 620) * 5 / 16;
            // Issue #80: if the device booted with the head physically
            // pushed below the safe range (e.g. previous unsafe firmware
            // or manual handling), don't carry that negative starting
            // angle into motion interpolation — clamp before storing so
            // subsequent interpolation runs only over safe positions.
            if (restored_pitch < SAFE_PITCH_MIN) restored_pitch = SAFE_PITCH_MIN;
            if (restored_pitch > SAFE_PITCH_MAX) restored_pitch = SAFE_PITCH_MAX;
            pitch_motion_.current_deg = restored_pitch;
            ESP_LOGI(TAG, "Restored pitch_motion_.current_deg=%d from ReadPos=%d (clamped to safe range %d..%d)",
                     pitch_motion_.current_deg, pitch_pos_actual, SAFE_PITCH_MIN, SAFE_PITCH_MAX);
        } else {
            // Issue #138: seed pitch current_deg to the neutral pose.
            // Without this, the boot-init `WriteHeadAngles(neutral, ...)`
            // interpolation below would start from the struct-default
            // `current_deg=0` (== pos=620 at deg=0, the lower mechanical
            // end-stop) and walk WritePos calls upward (pos=620, 623,
            // 626, ...) through end-stop-adjacent positions before
            // reaching the target -- risking servo bus degradation if
            // the SCS0009 wakes up mid-sequence. neutral_pitch_ is the
            // NVS-resolved rest pose (default BOOT_INIT_PITCH_DEG, always
            // within [SAFE_PITCH_MIN, SAFE_PITCH_MAX]); seeding to it
            // makes the subsequent interpolation a near-no-op
            // (start_deg == target_deg) which keeps the servo away from
            // end-stop territory throughout the wake-up window. This is
            // the firmware-side counterpart to the Phase 1a ReadPos
            // retry: retry absorbs the typical wake-up case so the
            // snap-suppress hold can fire; this seed handles the residual
            // case where wake-up exceeds the retry budget or the bus is
            // genuinely hung (#100).
            pitch_motion_.current_deg = neutral_pitch_;
            ESP_LOGW(TAG,
                     "Failed to ReadPos(pitch) after %d attempts; "
                     "seeded current_deg=%d (neutral_pitch_) "
                     "to avoid end-stop walk during boot-init climb",
                     BOOT_READPOS_MAX_ATTEMPTS, neutral_pitch_);
        }

        BaseType_t ok = xTaskCreate(&StackChanBoard::ServoTaskTrampoline,
                                    "servo_motion", 4096, this, 5,
                                    &servo_task_handle_);
        if (ok != pdPASS) {
            ESP_LOGE(TAG, "Failed to create servo_motion task; disabling servo");
            if (motion_mutex_ != nullptr) {
                vSemaphoreDelete(motion_mutex_);
                motion_mutex_ = nullptr;
            }
            if (scs_bus_mutex_ != nullptr) {
                vSemaphoreDelete(scs_bus_mutex_);
                scs_bus_mutex_ = nullptr;
            }
            motion_driver_.reset();
            servo_task_handle_ = nullptr;
            servo_ok_ = false;
            return;
        }

        // Issue #115: boot-time initialization to a fall-safe neutral
        // pose. Without this, the head retains whatever angle it was
        // left at on power-down — including end-stop positions (e.g.
        // pitch=0) that trigger the SCS0009 bus-hang documented in
        // #100 on the very first user-driven motion. By the time any
        // MCP command can arrive, we want the head already moved to
        // the center of the M5Stack-recommended 5..85° pitch range,
        // well clear of both mechanical end-stops.
        //
        // Design follows the goHome() pattern in m5stack/StackChan
        // (apps/app_setup/workers/servo.cpp:144) where the setup
        // app calls motion.goHome(speed) at boot, and the 1-second
        // positioning timing established in mongonta0716/stackchan-
        // arduino attachServos(). 1000ms move via the existing
        // interpolating WriteHeadAngles path keeps frame-rate stall
        // currents low; the extra 100ms vTaskDelay covers servo
        // settling before any subsequent motion can arrive.
        //
        // Implements #99 Option C and the boot-init aspect of #100
        // direction E. Existing pitch guards (#80 / #98 / #109)
        // continue to apply unchanged.
        // BOOT_INIT_YAW_DEG / BOOT_INIT_PITCH_DEG / BOOT_INIT_MOVE_MS
        // are class-level static constexpr; see the comment block at
        // their declaration above the YawDegToPos helper for the full
        // rationale (Issue #115 target pose, Issue #121 Problem 2
        // slower climb, Issue #138 promotion to class scope for the
        // safe-fallback seed in Phase 2 above).
        // Compute Phase 0 duration from the current_deg → target
        // deltas at BOOT_INIT_TARGET_DEG_PER_SEC, floored at
        // BOOT_INIT_MOVE_MS to keep the SCS0009 wake-up latency
        // window covered on the PMIC OFF/ON path (where Phase 0
        // is a no-op of effect but the BOOT_INIT_MOVE_MS budget
        // still needs to elapse before Phase 0' ReadPos). Without
        // this calculation, a yaw-90° (or any large pre-power-off
        // angle) prior set-point would run the boot-init yaw
        // motion at 30 deg/s+, exceeding the 15 deg/s cap.
        int phase0_yaw_delta;
        int phase0_pitch_delta;
        phase0_yaw_delta =
            neutral_yaw_ - static_cast<int>(motion_driver_->GetYawDeg());
        phase0_pitch_delta =
            neutral_pitch_ - static_cast<int>(motion_driver_->GetPitchDeg());
        if (phase0_yaw_delta < 0) phase0_yaw_delta = -phase0_yaw_delta;
        if (phase0_pitch_delta < 0) phase0_pitch_delta = -phase0_pitch_delta;
        int phase0_max_delta = phase0_yaw_delta > phase0_pitch_delta
            ? phase0_yaw_delta : phase0_pitch_delta;
        uint32_t phase0_duration_ms =
            (uint32_t)phase0_max_delta * 1000U /
            BOOT_INIT_TARGET_DEG_PER_SEC;
        if (phase0_duration_ms < BOOT_INIT_MOVE_MS) {
            phase0_duration_ms = BOOT_INIT_MOVE_MS;
        }
        TickType_t boot_init_start_tick = xTaskGetTickCount();
        WriteHeadAngles(neutral_yaw_, neutral_pitch_,
                        phase0_duration_ms,
                        /* prefer_linear = */ true);
        // Two-phase boot-init wait:
        //
        // (1) Mandatory minimum: vTaskDelay until
        //     phase0_duration_ms + 100 ms has elapsed. This must
        //     run UNCONDITIONALLY — independent of IsMoving() —
        //     because phase0_duration_ms is floored to
        //     BOOT_INIT_MOVE_MS specifically to cover the
        //     SCS0009 wake-up window on the PMIC OFF/ON path,
        //     even when WriteHeadAngles is a no-op (Phase 2
        //     safe-fallback seeded current_deg to exactly the
        //     boot target → motion_driver_->IsMoving() == false
        //     immediately → Phase 0' ReadPos would otherwise run
        //     inside the wake-up latency window and exhaust).
        //
        // (2) Optional extension: while motion_driver_->IsMoving()
        //     reports a motion still in flight, keep waiting up
        //     to a safety deadline. This covers the ServoDelegated
        //     path where the actual servo motion can start late
        //     due to dispatch retry latency (max 5 ×
        //     MOTION_POLL_INTERVAL_MS = 250 ms) — phase (1)'s
        //     budget may finish before the servo has completed
        //     its internal interpolation in that worst case.
        TickType_t boot_init_min_deadline =
            xTaskGetTickCount() +
            pdMS_TO_TICKS(phase0_duration_ms + 100);
        // Safety extension covers the worst-case ServoDelegated
        // dispatch latency. Each retry round dispatches yaw and
        // pitch sequentially under scs_bus_mutex_, so one fully-
        // timing-out round on both axes costs approximately:
        //   MOTION_POLL_INTERVAL_MS (50 ms tick wake)
        // + yaw WritePos ACK timeout (~100 ms; SCSCL's
        //   SCSerial::IOTimeOut = 100 ms, FeetechScs comparable)
        // + kInterFrameGap (10 ms)
        // + pitch WritePos ACK timeout (~100 ms)
        // ≈ 260 ms per retry round.
        //
        // 4 failing rounds + 1 successful round bound the
        // worst-case dispatch latency at roughly 4 × 260 ≈ 1040 ms.
        // Add a settle margin so the wait outlasts a genuine
        // delayed dispatch instead of breaking while IsMoving()
        // is legitimately true. 2000 ms covers the full retry
        // budget plus margin.
        //
        // HostInterpolation path is unaffected (dispatch latency
        // is 0 there; the wait exits well before this deadline
        // regardless).
        TickType_t boot_init_safety_deadline =
            boot_init_min_deadline + pdMS_TO_TICKS(2000);
        while ((int32_t)(xTaskGetTickCount() -
                         boot_init_min_deadline) < 0) {
            vTaskDelay(pdMS_TO_TICKS(50));
        }
        while (motion_driver_->IsMoving()) {
            if ((int32_t)(xTaskGetTickCount() -
                          boot_init_safety_deadline) >= 0) {
                ESP_LOGW(TAG,
                         "Boot init Phase 0 wait safety deadline elapsed while motion_driver_ still reports moving; proceeding to Phase 0' ReadPos anyway");
                break;
            }
            vTaskDelay(pdMS_TO_TICKS(50));
        }

        // Issue #123: capture post-init ReadPos so the boot-init effect
        // is observable in the serial log. ServoTask is now running, so
        // hold scs_bus_mutex_ across the ReadPos pair.
        //
        // Retry budget mirrors Phase 1a / 1b and the get_head_angles
        // MCP-tool retry. A transient `ReadPos == -1` on a healthy
        // servo would otherwise cause Phase 0' re-sync to skip,
        // leaving current_deg slightly stale for the session.
        int post_yaw_pos = -1;
        int post_pitch_pos = -1;
        int post_yaw_attempts = 0;
        int post_pitch_attempts = 0;
        xSemaphoreTake(scs_bus_mutex_, portMAX_DELAY);
        for (int i = 0; i < BOOT_READPOS_MAX_ATTEMPTS; ++i) {
            post_yaw_attempts = i + 1;
            post_yaw_pos = scs_bus_.ReadPos(SERVO_YAW_ID);
            if (post_yaw_pos >= 0) break;
            if (i + 1 < BOOT_READPOS_MAX_ATTEMPTS) {
                vTaskDelay(pdMS_TO_TICKS(BOOT_READPOS_RETRY_MS));
            }
        }
        for (int i = 0; i < BOOT_READPOS_MAX_ATTEMPTS; ++i) {
            post_pitch_attempts = i + 1;
            post_pitch_pos = scs_bus_.ReadPos(SERVO_PITCH_ID);
            if (post_pitch_pos >= 0) break;
            if (i + 1 < BOOT_READPOS_MAX_ATTEMPTS) {
                vTaskDelay(pdMS_TO_TICKS(BOOT_READPOS_RETRY_MS));
            }
        }
        xSemaphoreGive(scs_bus_mutex_);
        TickType_t boot_init_end_tick = xTaskGetTickCount();
        ESP_LOGI(TAG,
                 "Boot-time servo init complete: target yaw=%d pitch=%d "
                 "(move=%ums), post-ReadPos: yaw_raw=%d (attempts=%d) "
                 "pitch_raw=%d (attempts=%d), elapsed_ms=%u",
                 BOOT_INIT_YAW_DEG, BOOT_INIT_PITCH_DEG,
                 (unsigned)phase0_duration_ms,
                 post_yaw_pos, post_yaw_attempts,
                 post_pitch_pos, post_pitch_attempts,
                 (unsigned)((boot_init_end_tick - boot_init_start_tick) *
                            portTICK_PERIOD_MS));

        // Phase 0': mandatory current_deg re-sync from post-init
        // ReadPos before boot-time servo initialization completes.
        //
        // Background: on the PMIC long-press OFF / ON path, Phase 1
        // ReadPos retries can exhaust the budget while the SCS0009
        // is still in its wake-up latency window; Phase 2 then
        // seeds current_deg with BOOT_INIT_*_DEG so the Phase 0
        // interpolation is a no-op of effect. By the time the
        // BOOT_INIT_MOVE_MS-long vTaskDelay above has elapsed,
        // the SCS0009 has been powered for several seconds and a
        // ReadPos here is almost certain to succeed (the
        // observed boot log shows yaw_raw / pitch_raw populated
        // at tick ≥ ~6 s).
        //
        // Re-syncing current_deg with the actual physical position
        // now keeps the next move_head interpolation anchored to
        // where the servo really is, rather than to the Phase 2
        // safe-fallback seed.
        //
        // If a post-init ReadPos still fails, leave current_deg as
        // restored-or-seeded by Phase 2. That is safer than issuing
        // additional boot-time WritePos commands on a degraded bus.
        if (post_yaw_pos >= 0) {
            int actual_yaw_deg = (post_yaw_pos - 460) * 5 / 16;
            xSemaphoreTake(motion_mutex_, portMAX_DELAY);
            if (yaw_motion_.current_deg != actual_yaw_deg) {
                int prev_yaw_deg = yaw_motion_.current_deg;
                yaw_motion_.current_deg = actual_yaw_deg;
                yaw_motion_.start_deg = actual_yaw_deg;
                yaw_motion_.target_deg = actual_yaw_deg;
                yaw_motion_.moving = false;
                yaw_motion_.move_start_ms =
                    (uint32_t)(boot_init_end_tick * portTICK_PERIOD_MS);
                // Bump the driver's request_token so any Tick()
                // snapshot taken before this re-sync no longer
                // passes the post-bus freshness guard and does
                // not overwrite the just-re-synced state. The
                // delegated driver also clears its per-axis
                // private cancellation state under the same
                // motion_mutex_ hold.
                motion_driver_->InvalidateAxisToken(SERVO_YAW_ID);
                xSemaphoreGive(motion_mutex_);
                ESP_LOGI(TAG,
                         "Phase 0' yaw re-sync: current_deg %d -> %d "
                         "(actual ReadPos=%d)",
                         prev_yaw_deg, actual_yaw_deg, post_yaw_pos);
            } else {
                xSemaphoreGive(motion_mutex_);
            }
        } else {
            // Phase 0' ReadPos failed: current_deg holds the
            // Phase 2 restored-or-seeded value, which has NOT
            // been physically verified. Mark the axis position
            // unknown so the ServoDelegated path's no-op gate
            // does not silently skip a same-target recovery
            // command. The HostInterpolation path ignores this
            // flag; on that path the next WriteHeadAngles still
            // dispatches a fresh interpolation as before.
            xSemaphoreTake(motion_mutex_, portMAX_DELAY);
            yaw_motion_.position_unknown = true;
            // Token/state invalidation covers the position_unknown
            // mutation using the same external-reset boundary as
            // the success branch above.
            motion_driver_->InvalidateAxisToken(SERVO_YAW_ID);
            xSemaphoreGive(motion_mutex_);
            ESP_LOGW(TAG,
                     "Phase 0' yaw re-sync skipped: ReadPos failed; "
                     "leaving current_deg at restored-or-seeded value "
                     "and marking position unknown for delegated no-op gate");
        }
        if (post_pitch_pos >= 0) {
            int actual_pitch_deg = (post_pitch_pos - 620) * 5 / 16;
            if (actual_pitch_deg < SAFE_PITCH_MIN) actual_pitch_deg = SAFE_PITCH_MIN;
            if (actual_pitch_deg > SAFE_PITCH_MAX) actual_pitch_deg = SAFE_PITCH_MAX;
            xSemaphoreTake(motion_mutex_, portMAX_DELAY);
            if (pitch_motion_.current_deg != actual_pitch_deg) {
                int prev_pitch_deg = pitch_motion_.current_deg;
                pitch_motion_.current_deg = actual_pitch_deg;
                pitch_motion_.start_deg = actual_pitch_deg;
                pitch_motion_.target_deg = actual_pitch_deg;
                pitch_motion_.moving = false;
                pitch_motion_.move_start_ms =
                    (uint32_t)(boot_init_end_tick * portTICK_PERIOD_MS);
                // Invalidate the driver's token/state boundary
                // (see the yaw branch above for the rationale).
                motion_driver_->InvalidateAxisToken(SERVO_PITCH_ID);
                xSemaphoreGive(motion_mutex_);
                ESP_LOGI(TAG,
                         "Phase 0' pitch re-sync: current_deg %d -> %d "
                         "(actual ReadPos=%d, clamped to safe range %d..%d)",
                         prev_pitch_deg, actual_pitch_deg, post_pitch_pos,
                         SAFE_PITCH_MIN, SAFE_PITCH_MAX);
            } else {
                xSemaphoreGive(motion_mutex_);
            }
        } else {
            xSemaphoreTake(motion_mutex_, portMAX_DELAY);
            pitch_motion_.position_unknown = true;
            // Invalidate the driver's token/state boundary for the
            // position_unknown mutation (see the yaw fail branch
            // above).
            motion_driver_->InvalidateAxisToken(SERVO_PITCH_ID);
            xSemaphoreGive(motion_mutex_);
            ESP_LOGW(TAG,
                     "Phase 0' pitch re-sync skipped: ReadPos failed; "
                     "leaving current_deg at restored-or-seeded value "
                     "and marking position unknown for delegated no-op gate");
        }
        boot_init_done_.store(true, std::memory_order_release);
    }

}

void StackChanBoard::WriteHeadAngles(int yaw_deg, int pitch_deg, uint32_t duration_ms, bool prefer_linear) {

    if (!servo_ok_ || motion_driver_ == nullptr) {
        ESP_LOGW(TAG, "WriteHeadAngles skipped: servo not initialized");
        return;
    }
    if (!TakeMotionMutexAfterTorqueEngaged()) {
        return;
    }
    if (servo_wobble_active_.load()) {
        servo_wobble_active_.store(false);
        servo_wobble_step_.store(0);
    }
    motion_driver_->StartMove(yaw_deg, pitch_deg, duration_ms,
                              prefer_linear);
    xSemaphoreGive(motion_mutex_);

}

void StackChanBoard::WriteHeadAngles(int yaw_deg, int pitch_deg, int speed_dps) {

    if (!servo_ok_ || motion_driver_ == nullptr) {
        ESP_LOGW(TAG, "WriteHeadAngles(speed_dps) skipped: servo not initialized");
        return;
    }
    int safe_speed = speed_dps;
    if (safe_speed <= 0) {
        safe_speed = DEFAULT_SPEED_DPS;
    } else if (safe_speed < MIN_SMOOTH_SPEED_DPS) {
        // Below the on-device measured smoothness floor -- motion will look
        // textured on SCS0009 at MOTION_TICK_MS=20 ms. This is intentionally
        // permitted (the gateway "low" preset is 30 dps, deliberately below
        // the floor for slow, expressive motion per Issue #129 design).
        // Log once so callers can see they are below the smooth zone.
        ESP_LOGW(TAG, "WriteHeadAngles: speed_dps=%d below MIN_SMOOTH_SPEED_DPS=%d (textured motion is expected)",
                 speed_dps, MIN_SMOOTH_SPEED_DPS);
    } else if (safe_speed > MAX_SPEED_DPS) {
        ESP_LOGW(TAG, "WriteHeadAngles: speed_dps=%d above MAX_SPEED_DPS=%d, clamping",
                 speed_dps, MAX_SPEED_DPS);
        safe_speed = MAX_SPEED_DPS;
    }

    int yaw_delta = std::abs(yaw_deg - static_cast<int>(motion_driver_->GetYawDeg()));
    int pitch_delta = std::abs(pitch_deg - static_cast<int>(motion_driver_->GetPitchDeg()));
    int max_delta = std::max(yaw_delta, pitch_delta);
    uint32_t duration_ms = std::max<uint32_t>(
        MOTION_TICK_MS,
        static_cast<uint32_t>(max_delta) * 1000U / static_cast<uint32_t>(safe_speed));
    WriteHeadAngles(yaw_deg, pitch_deg, duration_ms);

}

void StackChanBoard::ServoWobbleStepAdvance() {

    if (!servo_ok_ || motion_driver_ == nullptr) {
        return;
    }
    if (!servo_wobble_active_.load(std::memory_order_acquire)) {
        return;
    }
    if (!TakeMotionMutexAfterTorqueEngaged()) {
        return;
    }
    if (!servo_wobble_active_.load()) {
        xSemaphoreGive(motion_mutex_);
        return;
    }
    // Direct AxisMotion field access under motion_mutex_; calling
    // motion_driver_->IsMoving() here would re-take the non-recursive
    // semaphore and deadlock.
    bool moving = yaw_motion_.moving || pitch_motion_.moving;
    if (moving) {
        xSemaphoreGive(motion_mutex_);
        return;
    }
    const int A = SERVO_WOBBLE_AMPLITUDE_DEG;
    int step = servo_wobble_step_.load();
    int target_yaw = 0;
    // Preserve the current pitch through the wobble sequence; only the
    // yaw axis is animated. A hardcoded `target_pitch = 0` on every
    // step would command the SCS0009 pitch axis toward the lower
    // end-stop (raw pos ~620 ≈ pitch 0°) on a device whose standard
    // rest pose is BOOT_INIT_PITCH_DEG=45°, accelerating #165
    // cumulative WritePos protection-mode onset within a single
    // STROKE gesture. Direct field read is safe here because
    // motion_mutex_ is already held (see contract above). #175.
    int target_pitch = pitch_motion_.current_deg;
    switch (step) {
        case 0: target_yaw = -A; break;
        case 1: target_yaw = +A; break;
        case 2: target_yaw = -A; break;
        case 3: target_yaw =  0; break;
        default:
            servo_wobble_active_.store(false);
            xSemaphoreGive(motion_mutex_);
            return;
    }
    // StartMove contract: caller holds motion_mutex_. Direct call
    // (not via WriteHeadAngles) avoids the re-entrant mutex take.
    motion_driver_->StartMove(target_yaw, target_pitch, SERVO_WOBBLE_STEP_MS);
    servo_wobble_step_.store(step + 1);
    if (step + 1 > 3) {
        servo_wobble_active_.store(false);
    }
    xSemaphoreGive(motion_mutex_);

}

void StackChanBoard::StartServoWobble() {

    if (!servo_ok_ || motion_driver_ == nullptr) {
        ESP_LOGW(TAG, "Servo wobble skipped: servo not initialized");
        return;
    }
    // Stage the wobble; the actual ServoWobbleStepAdvance() is
    // performed on servo_motion task next tick (see ServoTaskMain).
    // Calling ServoWobbleStepAdvance() directly from here — which is
    // commonly reached via the ESP_TIMER_TASK touch-poll callback —
    // would race with the servo_motion task's own per-tick
    // ServoWobbleStepAdvance() at idle: the atomic step load/store
    // does not exclude the read-switch-store compound operation, so
    // two callers can both observe step==0 and end up dispatching
    // step 0 and step 1 concurrently. Centralising advance on the
    // servo_motion task makes the step sequence deterministic, at
    // the cost of a single MOTION_POLL_INTERVAL_MS / MOTION_TICK_MS
    // delay on the first wobble step (well under human perception).
    //
    // motion_mutex_ also serialises this restart against a
    // concurrent ServoWobbleStepAdvance() running on servo_motion:
    // without the hold, a final-step advancement finishing at the
    // same moment as this restart could overwrite our step=0 /
    // active=true with step+1 or active=false, silently dropping
    // the new stroke's wobble. The mutex is staging-only (no UART
    // I/O is performed under it), so taking it from ESP_TIMER_TASK
    // is bounded to sub-millisecond hold time.
    xSemaphoreTake(motion_mutex_, portMAX_DELAY);
    servo_wobble_step_.store(0);
    servo_wobble_active_.store(true);
    xSemaphoreGive(motion_mutex_);

}

void StackChanBoard::ServoTaskTrampoline(void* arg) {

    static_cast<StackChanBoard*>(arg)->ServoTaskMain();

}

void StackChanBoard::ServoTaskMain() {

    while (true) {
        if (!servo_ok_ || motion_driver_ == nullptr) {
            vTaskDelay(pdMS_TO_TICKS(MOTION_TICK_MS));
            continue;
        }
        motion_driver_->Tick();
        MaybeAutoReleaseTorque();
        ServoWobbleStepAdvance();
        taskYIELD();
    }

}
