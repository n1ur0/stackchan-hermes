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
// used by this board, but their WritePos success value differs (see
// ServoWritePosOk() below). The rest of stackchan.cc treats both drivers
// uniformly through the ScsBus type alias plus that helper.
#if CONFIG_STACKCHAN_SERVO_FEETECH
#include "feetech_scs.h"
using ScsBus = FeetechScs;
// FeetechScs::WritePos returns 0 on ACK and -1 on bus error.
static inline bool ServoWritePosOk(int r) { return r >= 0; }
#else
#include "SCSCL.h"
using ScsBus = SCSCL;
// SCSCL::WritePos returns 1 on ACK, 0 on ACK timeout, -1 on bus error.
// Treat ACK timeout as failure to keep the original behaviour intact.
static inline bool ServoWritePosOk(int r) { return r > 0; }
#endif
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

const char* StackChanBoard::ReleaseReasonName(ReleaseReason reason) {

    switch (reason) {
        case ReleaseReason::kManual:
            return "manual";
        case ReleaseReason::kAutoIdle:
            return "auto_idle";
        case ReleaseReason::kReengagement:
            return "reengagement";
    }
    return "unknown";

}

void StackChanBoard::PublishTorqueState() {

    TorqueState state;
    if (yaw_torque_enabled_ && pitch_torque_enabled_) {
        state = TorqueState::kEngaged;
    } else if (!yaw_torque_enabled_ && !pitch_torque_enabled_) {
        state = TorqueState::kReleased;
    } else {
        state = TorqueState::kPartial;
    }
    TorqueState old_state =
        torque_state_.load(std::memory_order_acquire);
    if (state == TorqueState::kEngaged &&
        old_state != TorqueState::kEngaged) {
        // Reset the ServoTask-owned idle window even when OFF->ON
        // happens between ServoTask ticks.
        idle_timer_reset_pending_.store(true,
                                        std::memory_order_release);
    }
    torque_state_.store(state, std::memory_order_release);

}

uint32_t StackChanBoard::MarkReleasing() {

    uint32_t epoch =
        torque_release_epoch_.fetch_add(1, std::memory_order_acq_rel) +
        1;
    torque_state_.store(TorqueState::kReleasing,
                        std::memory_order_release);
    return epoch;

}

bool StackChanBoard::WaitForKReleasingToClear() {

    constexpr uint32_t kMaxKReleasingWaitMs = 200;
    constexpr uint32_t kKReleasingPollIntervalMs = 5;
    const TickType_t kDelayTicks =
        std::max<TickType_t>(1,
                             pdMS_TO_TICKS(kKReleasingPollIntervalMs));
    const uint32_t start_us =
        static_cast<uint32_t>(esp_timer_get_time());
    auto state = torque_state_.load(std::memory_order_acquire);
    while (state == TorqueState::kReleasing) {
        const uint32_t elapsed_ms =
            (static_cast<uint32_t>(esp_timer_get_time()) - start_us) /
            1000;
        if (elapsed_ms >= kMaxKReleasingWaitMs) {
            return false;
        }
        vTaskDelay(kDelayTicks);
        state = torque_state_.load(std::memory_order_acquire);
    }
    return true;

}

ServoTorqueResult StackChanBoard::InternalSetServoTorque(bool yaw_enabled, bool pitch_enabled, ReleaseReason reason, uint32_t expected_release_epoch = 0) {

    ServoTorqueResult result;
    const bool disables_axis = !yaw_enabled || !pitch_enabled;

    auto update_bus_ok = [&]() {
#if CONFIG_STACKCHAN_SERVO_FEETECH
        result.yaw_ok = (result.yaw_bus_return == 0);
        result.pitch_ok = (result.pitch_bus_return == 0);
#else
        result.yaw_ok = (result.yaw_bus_return > 0);
        result.pitch_ok = (result.pitch_bus_return > 0);
#endif
    };

    // Issue #171: classify each exit path with a single 3-valued tag so
    // that idempotent_short_circuit and wait_exhausted can never both be
    // set. A one-bool flag could not express the two orthogonal outcomes;
    // a single enum makes "both true" structurally unrepresentable.
    //   * kBusAction:     a real EnableTorque() bus write was attempted
    //                     (or the servo subsystem was unavailable); the
    //                     outcome is carried by yaw_ok/pitch_ok. Neither
    //                     short-circuit flag is set.
    //   * kIdempotent:    returned without a bus frame, state already
    //                     matched the request (success no-op).
    //   * kWaitExhausted: returned without a bus frame, the kReleasing
    //                     wait budget was exhausted (failure).
    enum class ExitKind { kBusAction, kIdempotent, kWaitExhausted };

    auto log_result = [&](ExitKind kind) {
        result.idempotent_short_circuit = (kind == ExitKind::kIdempotent);
        result.wait_exhausted = (kind == ExitKind::kWaitExhausted);
        // Defensive: the enum makes this impossible, but assert anyway so
        // any future direct field mutation is caught in debug builds.
        assert(!(result.idempotent_short_circuit && result.wait_exhausted));
        ESP_LOGI(TAG,
                 "set_servo_torque (reason=%s): servo_ok=%d "
                 "yaw_enabled=%d (r=%d) pitch_enabled=%d (r=%d) "
                 "idempotent_short_circuit=%d wait_exhausted=%d",
                 ReleaseReasonName(reason),
                 servo_ok_ ? 1 : 0,
                 yaw_enabled ? 1 : 0, result.yaw_bus_return,
                 pitch_enabled ? 1 : 0, result.pitch_bus_return,
                 result.idempotent_short_circuit ? 1 : 0,
                 result.wait_exhausted ? 1 : 0);
    };

    auto finish = [&](ExitKind kind) -> ServoTorqueResult {
        log_result(kind);
        return result;
    };

    auto publish_after_bus_attempt = [&](TorqueState pre_bus_state) {
        const bool any_axis_bus_failed =
            !result.yaw_ok || !result.pitch_ok;
        if (!any_axis_bus_failed) {
            // Success-path cached-state ownership is pre-existing and
            // tracked separately under Issue #172.
            PublishTorqueState();
            return;
        }
        TorqueState expected = pre_bus_state;
        if (torque_state_.compare_exchange_strong(
                expected,
                TorqueState::kUncertain,
                std::memory_order_release,
                std::memory_order_acquire)) {
            ESP_LOGW(TAG,
                     "set_servo_torque (reason=%s): publishing kUncertain; "
                     "bus confirmation failed for yaw_failed=%d (r=%d) "
                     "pitch_failed=%d (r=%d)",
                     ReleaseReasonName(reason),
                     result.yaw_ok ? 0 : 1, result.yaw_bus_return,
                     result.pitch_ok ? 0 : 1, result.pitch_bus_return);
        } else {
            ESP_LOGW(TAG,
                     "set_servo_torque (reason=%s): kUncertain publish "
                     "skipped; torque_state_ advanced from %d to %d "
                     "(likely MarkReleasing()); leaving concurrent state "
                     "intact. bus_return yaw=%d pitch=%d",
                     ReleaseReasonName(reason),
                     static_cast<int>(pre_bus_state),
                     static_cast<int>(expected),
                     result.yaw_bus_return, result.pitch_bus_return);
        }
    };

    if (!servo_ok_ || scs_bus_mutex_ == nullptr) {
        // Servo subsystem unavailable: not a short-circuit and not a
        // wait timeout. yaw_ok/pitch_ok stay false, so ok is false.
        return finish(ExitKind::kBusAction);
    }

    // Fully-symmetric re-engage remains bus-ordered even when it becomes
    // a no-op. If an auto-idle OFF has already published kReleasing, the
    // manual path waits for that pending transition before entering the
    // bus section.
    if (yaw_enabled && pitch_enabled) {
        // Pre-mutex wait: this reduces obvious contention before
        // attempting scs_bus_mutex_. The post-mutex re-check below closes
        // the pre-check-to-mutex TOCTOU window.
        if (reason == ReleaseReason::kManual) {
            auto state_pre =
                torque_state_.load(std::memory_order_acquire);
            if (state_pre == TorqueState::kReleasing) {
                if (!WaitForKReleasingToClear()) {
                    ESP_LOGW(TAG,
                             "set_servo_torque (reason=%s): kReleasing "
                             "not clearing within wait budget; skipping "
                             "bus frames, caller may retry.",
                             ReleaseReasonName(reason));
                    // Pre-mutex wait budget exhausted: no bus frame went
                    // out, the requested ON did not happen (Issue #171).
                    log_result(ExitKind::kWaitExhausted);
                    return result;
                }
                // After the wait, state may be kEngaged (OFF rolled
                // back), kReleased (OFF succeeded), or kUncertain (OFF
                // bus confirmation failed). Fall through to the existing
                // (true, true) logic, which short-circuits on kEngaged and
                // proceeds normally on kReleased/kPartial/kUncertain.
            }
        }

        // Bounded retry for the remaining TOCTOU window: torque_state_
        // can flip to kReleasing between the pre-mutex check and
        // xSemaphoreTake(). Once the bus mutex is held, re-check; if an
        // auto-release OFF was published in that gap, release the mutex,
        // wait, and retry. kReengagement is exempt because
        // EnsureTorqueEngagedBeforeMove() already waited; kAutoIdle does
        // not enter this (true, true) branch.
        for (int attempt = 0; attempt < kMaxManualReengageRetries;
             ++attempt) {
            xSemaphoreTake(scs_bus_mutex_, portMAX_DELAY);

            if (reason == ReleaseReason::kManual &&
                torque_state_.load(std::memory_order_acquire) ==
                    TorqueState::kReleasing) {
                xSemaphoreGive(scs_bus_mutex_);
                if (!WaitForKReleasingToClear()) {
                    ESP_LOGW(TAG,
                             "set_servo_torque (reason=%s): kReleasing "
                             "persists after attempt %d; skipping bus "
                             "frames, caller may retry.",
                             ReleaseReasonName(reason),
                             attempt);
                    // Post-mutex re-check wait budget exhausted: no bus
                    // frame, requested ON did not happen (Issue #171).
                    log_result(ExitKind::kWaitExhausted);
                    return result;
                }
                continue;
            }

            if (torque_state_.load(std::memory_order_acquire) ==
                TorqueState::kEngaged) {
                // Already engaged (reached here only after any kReleasing
                // wait already cleared): legitimate no-op success, so ok
                // stays true (Issue #171).
                log_result(ExitKind::kIdempotent);
                xSemaphoreGive(scs_bus_mutex_);
                return result;
            }
            const TorqueState pre_bus_state =
                torque_state_.load(std::memory_order_acquire);
            result.yaw_bus_return =
                scs_bus_.EnableTorque(SERVO_YAW_ID, 1);
            result.pitch_bus_return =
                scs_bus_.EnableTorque(SERVO_PITCH_ID, 1);
            update_bus_ok();
            if (result.yaw_ok) {
                yaw_torque_enabled_ = true;
            }
            if (result.pitch_ok) {
                pitch_torque_enabled_ = true;
            }
            publish_after_bus_attempt(pre_bus_state);
            // Real bus write attempted; ok is governed by yaw_ok/pitch_ok.
            log_result(ExitKind::kBusAction);
            xSemaphoreGive(scs_bus_mutex_);
            return result;
        }

        ESP_LOGW(TAG,
                 "set_servo_torque (reason=%s): kReleasing observed in "
                 "all %d post-mutex retries; skipping bus frames.",
                 ReleaseReasonName(reason),
                 kMaxManualReengageRetries);
        // All retries exhausted while still kReleasing: no bus frame, the
        // requested ON did not happen (Issue #171).
        log_result(ExitKind::kWaitExhausted);
        return result;
    } else {
        if (!yaw_enabled && !pitch_enabled) {
            xSemaphoreTake(scs_bus_mutex_, portMAX_DELAY);
            bool already_released =
                torque_state_.load(std::memory_order_acquire) ==
                TorqueState::kReleased;
            if (already_released) {
                // Already released for an OFF request: legitimate no-op
                // success, so ok stays true (Issue #171).
                log_result(ExitKind::kIdempotent);
                xSemaphoreGive(scs_bus_mutex_);
                return result;
            }
            xSemaphoreGive(scs_bus_mutex_);
        }

        // Preserve the existing cancellation-first disable path exactly:
        // reset MotionDriver state for axes being disabled before the
        // EnableTorque(OFF) bus frames can race with ServoTask writes.
        if (motion_driver_ != nullptr && motion_mutex_ != nullptr &&
            disables_axis) {
            xSemaphoreTake(motion_mutex_, portMAX_DELAY);
            if (reason == ReleaseReason::kAutoIdle &&
                (yaw_motion_.moving || pitch_motion_.moving ||
                 servo_wobble_active_.load(std::memory_order_acquire))) {
                // Auto-idle deferring because motion is still in progress:
                // a benign no-op (no wait budget consumed), not a timeout.
                xSemaphoreGive(motion_mutex_);
                return finish(ExitKind::kIdempotent);
            }
            if (!yaw_enabled) {
                yaw_motion_.moving = false;
                yaw_motion_.position_unknown = true;
                motion_driver_->InvalidateAxisToken(SERVO_YAW_ID);
            }
            if (!pitch_enabled) {
                pitch_motion_.moving = false;
                pitch_motion_.position_unknown = true;
                motion_driver_->InvalidateAxisToken(SERVO_PITCH_ID);
            }
            servo_wobble_active_.store(false, std::memory_order_release);
            servo_wobble_step_.store(0, std::memory_order_release);
            // Mark the fully-OFF transition before releasing
            // motion_mutex_ so concurrent motion entries do not observe
            // the old engaged state while the OFF bus write is pending.
            if (!yaw_enabled && !pitch_enabled) {
                (void)MarkReleasing();
            }
            xSemaphoreGive(motion_mutex_);
        }

        xSemaphoreTake(scs_bus_mutex_, portMAX_DELAY);
        if (reason == ReleaseReason::kAutoIdle &&
            expected_release_epoch != 0) {
            uint32_t current_epoch =
                torque_release_epoch_.load(std::memory_order_acquire);
            auto current_state =
                torque_state_.load(std::memory_order_acquire);
            if (current_epoch != expected_release_epoch ||
                current_state != TorqueState::kReleasing) {
                ESP_LOGW(TAG,
                         "auto-release OFF aborted at bus check: epoch=%u "
                         "(expected %u), state=%d (expected kReleasing); "
                         "skipping EnableTorque(0,0) frames.",
                         (unsigned)current_epoch,
                         (unsigned)expected_release_epoch,
                         (int)current_state);
                xSemaphoreGive(scs_bus_mutex_);
                // Stale auto-release OFF superseded by a newer epoch/state:
                // the OFF is already obsolete, a benign no-op (Issue #171).
                log_result(ExitKind::kIdempotent);
                return result;
            }
        }
        const TorqueState pre_bus_state =
            torque_state_.load(std::memory_order_acquire);
        result.yaw_bus_return = scs_bus_.EnableTorque(
            SERVO_YAW_ID, yaw_enabled ? 1 : 0);
        result.pitch_bus_return = scs_bus_.EnableTorque(
            SERVO_PITCH_ID, pitch_enabled ? 1 : 0);
        update_bus_ok();
        if (result.yaw_ok) {
            yaw_torque_enabled_ = yaw_enabled;
        }
        if (result.pitch_ok) {
            pitch_torque_enabled_ = pitch_enabled;
        }
        publish_after_bus_attempt(pre_bus_state);
        // Real bus write attempted; ok is governed by yaw_ok/pitch_ok.
        log_result(ExitKind::kBusAction);
        xSemaphoreGive(scs_bus_mutex_);
        return result;
    }

}

void StackChanBoard::EnsureTorqueEngagedBeforeMove() {

    auto state = torque_state_.load(std::memory_order_acquire);
    if (state == TorqueState::kEngaged) {
        return;
    }
    if (!servo_ok_) {
        return;
    }

    if (state == TorqueState::kReleasing) {
        if (!WaitForKReleasingToClear()) {
            ESP_LOGW(TAG,
                     "EnsureTorqueEngagedBeforeMove: kReleasing not "
                     "clearing within wait budget, deferring to caller "
                     "retry.");
            return;
        }
        state = torque_state_.load(std::memory_order_acquire);
        if (state == TorqueState::kEngaged) {
            return;  // OFF rolled back to kEngaged
        }
    }

    // state is kPartial, kReleased, or kUncertain -- safe to
    // re-engage now.
    InternalSetServoTorque(true, true, ReleaseReason::kReengagement);

}

bool StackChanBoard::TakeMotionMutexAfterTorqueEngaged() {

    for (int attempt = 0; attempt < kMaxReengageRetries; ++attempt) {
        EnsureTorqueEngagedBeforeMove();
        xSemaphoreTake(motion_mutex_, portMAX_DELAY);
        if (torque_state_.load(std::memory_order_acquire) ==
            TorqueState::kEngaged) {
            return true;
        }
        xSemaphoreGive(motion_mutex_);
        vTaskDelay(pdMS_TO_TICKS(MOTION_TICK_MS));
    }
    ESP_LOGW(TAG,
             "EnsureTorqueEngagedBeforeMove: re-engagement failed after "
             "%d attempts; skipping motion entry to avoid silent "
             "torque-off WritePos.",
             kMaxReengageRetries);
    return false;

}

void StackChanBoard::MaybeAutoReleaseTorque() {

    if (!auto_release_enabled_.load(std::memory_order_acquire)) {
        return;
    }
    if (!servo_ok_) {
        return;
    }
    if (!boot_init_done_.load(std::memory_order_acquire)) {
        return;
    }
    // PublishTorqueState() raises this when torque re-engages between
    // ServoTask ticks, so a stale idle timer cannot immediately re-OFF.
    if (idle_timer_reset_pending_.exchange(
            false, std::memory_order_acq_rel)) {
        last_motion_end_valid_ = false;
    }
    // Keep the idle window scoped to the currently engaged interval.
    // Released/partial/releasing states must not age a stale timer
    // into the next re-engage. kUncertain is treated as engaged for
    // auto-release purposes: if the kAutoIdle OFF bus frame was lost
    // on the UART path, the auto-release retry must continue so the
    // device does not strand with torque physically ON; if the OFF
    // was actually delivered, the next retry short-circuits via the
    // idempotent path (Issue #170 follow-up).
    auto current_state =
        torque_state_.load(std::memory_order_acquire);
    if (current_state != TorqueState::kEngaged &&
        current_state != TorqueState::kUncertain) {
        last_motion_end_valid_ = false;
        return;
    }
    if (servo_wobble_active_.load(std::memory_order_acquire)) {
        last_motion_end_valid_ = false;
        return;
    }

    bool moving = motion_driver_->IsMoving();
    uint32_t now_ms =
        static_cast<uint32_t>(esp_timer_get_time() / 1000);

    if (moving) {
        last_motion_end_valid_ = false;
        return;
    }

    if (!last_motion_end_valid_) {
        last_motion_end_ms_ = now_ms;
        last_motion_end_valid_ = true;
        return;
    }

    uint32_t idle_ms = now_ms - last_motion_end_ms_;
    uint32_t timeout_ms =
        auto_release_timeout_ms_.load(std::memory_order_acquire);
    if (idle_ms >= timeout_ms) {
        // Publish the pending OFF before InternalSetServoTorque() can
        // block on motion_mutex_, so a concurrent manual ON is routed
        // through the kReleasing wait/retry path instead of treating the
        // already-expired engaged state as a successful no-op.
        uint32_t my_pre_epoch = MarkReleasing();
        ServoTorqueResult r = InternalSetServoTorque(
            false, false, ReleaseReason::kAutoIdle,
            /*expected_release_epoch=*/my_pre_epoch + 1);
        auto state_after =
            torque_state_.load(std::memory_order_acquire);
        if (state_after == TorqueState::kReleased) {
            last_motion_end_valid_ = false;
        } else if (r.idempotent_short_circuit || r.wait_exhausted) {
            // Either short-circuit flag means the OFF returned without a
            // completed bus frame (Issue #171 split the old
            // short_circuited flag; for this kAutoIdle path only the
            // idempotent flag can fire, but the OR keeps the "no bus
            // action" intent explicit and future-proof).
            uint32_t current_epoch =
                torque_release_epoch_.load(std::memory_order_acquire);
            if (current_epoch == my_pre_epoch) {
                // Auto-idle re-observed motion or wobble under
                // motion_mutex_ and returned before any bus frame went
                // out, so the per-axis torque state is still fully
                // engaged.
                torque_state_.store(TorqueState::kEngaged,
                                    std::memory_order_release);
                ESP_LOGW(TAG,
                         "auto-release OFF aborted by motion/wobble "
                         "re-check; rolled back torque_state_ to "
                         "kEngaged (epoch=%u), retry after "
                         "one idle window.",
                         (unsigned)my_pre_epoch);
            } else if (current_epoch == my_pre_epoch + 1) {
                ESP_LOGW(TAG,
                         "auto-release OFF aborted at bus check; leaving "
                         "torque_state_ for concurrent publisher "
                         "(my_pre_epoch=%u current_epoch=%u).",
                         (unsigned)my_pre_epoch,
                         (unsigned)current_epoch);
            } else {
                ESP_LOGW(TAG,
                         "auto-release OFF: release epoch advanced past "
                         "ours (my_pre_epoch=%u current_epoch=%u); "
                         "leaving torque_state_ for concurrent publisher.",
                         (unsigned)my_pre_epoch,
                         (unsigned)current_epoch);
            }
            last_motion_end_ms_ = now_ms;
        } else {
            last_motion_end_ms_ = now_ms;
            ESP_LOGW(TAG,
                     "auto-release OFF bus write failed: yaw_ok=%d "
                     "(r=%d) pitch_ok=%d (r=%d). Retrying after one "
                     "idle window.",
                     r.yaw_ok ? 1 : 0, r.yaw_bus_return,
                     r.pitch_ok ? 1 : 0, r.pitch_bus_return);
        }
    }

}

