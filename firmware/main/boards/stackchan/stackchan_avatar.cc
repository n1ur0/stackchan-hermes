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

bool StackChanBoard::EnsureStatusLabel() {

    if (status_label_ != nullptr) {
        return true;
    }
    lv_obj_t* screen = lv_screen_active();
    if (screen == nullptr) {
        return false;
    }
    status_label_ = lv_label_create(screen);
    if (status_label_ == nullptr) {
        return false;
    }
    // Translucent black backing for legibility over the face. The label
    // inherits the screen's text font (the common puhui font with
    // Japanese glyphs), so no explicit font is set here.
    lv_obj_set_style_bg_color(status_label_, lv_color_black(), 0);
    lv_obj_set_style_bg_opa(status_label_, LV_OPA_60, 0);
    lv_obj_set_style_text_color(status_label_, lv_color_white(), 0);
    lv_obj_set_style_radius(status_label_, 8, 0);
    lv_obj_set_style_pad_left(status_label_, 8, 0);
    lv_obj_set_style_pad_right(status_label_, 8, 0);
    lv_obj_set_style_pad_top(status_label_, 3, 0);
    lv_obj_set_style_pad_bottom(status_label_, 3, 0);
    lv_obj_align(status_label_, LV_ALIGN_TOP_MID, 0, 6);
    lv_obj_clear_flag(status_label_, LV_OBJ_FLAG_SCROLLABLE);
    // Hidden until the first non-empty SetStatusText().
    lv_obj_add_flag(status_label_, LV_OBJ_FLAG_HIDDEN);
    lv_obj_move_foreground(status_label_);
    ESP_LOGI(TAG, "Status label created on active screen");
    return true;

}

bool StackChanBoard::SetStatusText(const char* text) {

    if (display_ == nullptr) {
        ESP_LOGW(TAG, "SetStatusText ignored: display_ not ready");
        return false;
    }
    const char* safe = (text != nullptr) ? text : "";
    DisplayLockGuard lock(display_);
    if (!EnsureStatusLabel()) {
        return false;
    }
    if (safe[0] == '\0') {
        lv_obj_add_flag(status_label_, LV_OBJ_FLAG_HIDDEN);
        // Hidden labels don't invalidate themselves; redraw the area they
        // vacated so the caption clears without waiting for the next touch
        // (CoreS3 has no periodic LVGL refresh while listening).
        lv_obj_invalidate(lv_obj_get_parent(status_label_));
    } else {
        lv_label_set_text(status_label_, safe);
        lv_obj_clear_flag(status_label_, LV_OBJ_FLAG_HIDDEN);
        lv_obj_move_foreground(status_label_);
        // Force a deterministic same-frame flush. Without this the new
        // caption is invisible until the next event triggers a redraw,
        // which reads as a one-turn lag. layout first so a freshly
        // un-hidden label has a non-zero area to invalidate.
        lv_obj_update_layout(status_label_);
        lv_obj_invalidate(status_label_);
    }
    lv_refr_now(lv_obj_get_display(status_label_));
    return true;

}

bool StackChanBoard::EnsureSubtitleLabel() {

    if (subtitle_label_ != nullptr) {
        return true;
    }
    lv_obj_t* screen = lv_screen_active();
    if (screen == nullptr) {
        return false;
    }
    subtitle_label_ = lv_label_create(screen);
    if (subtitle_label_ == nullptr) {
        return false;
    }
    // Wrap long sentences across lines instead of overflowing the screen
    // width. The fixed width (300 of the 320 px LCD) plus a max height of
    // ~3 lines keeps the box to 2-3 wrapped lines; extra text is clipped.
    lv_label_set_long_mode(subtitle_label_, LV_LABEL_LONG_MODE_WRAP);
    lv_obj_set_width(subtitle_label_, 300);
    lv_obj_set_style_max_height(subtitle_label_, 78, 0);
    lv_obj_set_style_text_align(subtitle_label_, LV_TEXT_ALIGN_CENTER, 0);
    // Same translucent black backing as status_label_ for legibility.
    lv_obj_set_style_bg_color(subtitle_label_, lv_color_black(), 0);
    lv_obj_set_style_bg_opa(subtitle_label_, LV_OPA_60, 0);
    lv_obj_set_style_text_color(subtitle_label_, lv_color_white(), 0);
    lv_obj_set_style_radius(subtitle_label_, 8, 0);
    lv_obj_set_style_pad_left(subtitle_label_, 8, 0);
    lv_obj_set_style_pad_right(subtitle_label_, 8, 0);
    lv_obj_set_style_pad_top(subtitle_label_, 3, 0);
    lv_obj_set_style_pad_bottom(subtitle_label_, 3, 0);
    lv_obj_align(subtitle_label_, LV_ALIGN_BOTTOM_MID, 0, -6);
    lv_obj_clear_flag(subtitle_label_, LV_OBJ_FLAG_SCROLLABLE);
    // Hidden until the first non-empty SetSubtitleText().
    lv_obj_add_flag(subtitle_label_, LV_OBJ_FLAG_HIDDEN);
    lv_obj_move_foreground(subtitle_label_);
    ESP_LOGI(TAG, "Subtitle label created on active screen");
    return true;

}

bool StackChanBoard::SetSubtitleText(const char* text) {

    if (display_ == nullptr) {
        ESP_LOGW(TAG, "SetSubtitleText ignored: display_ not ready");
        return false;
    }
    const char* safe = (text != nullptr) ? text : "";
    DisplayLockGuard lock(display_);
    if (!EnsureSubtitleLabel()) {
        return false;
    }
    if (safe[0] == '\0') {
        lv_obj_add_flag(subtitle_label_, LV_OBJ_FLAG_HIDDEN);
        // Redraw the vacated area (the hidden label can't invalidate
        // itself) so the subtitle clears immediately. See SetStatusText().
        lv_obj_invalidate(lv_obj_get_parent(subtitle_label_));
    } else {
        lv_label_set_text(subtitle_label_, safe);
        lv_obj_clear_flag(subtitle_label_, LV_OBJ_FLAG_HIDDEN);
        lv_obj_move_foreground(subtitle_label_);
        // Same-frame flush so the subtitle appears without a one-turn lag.
        lv_obj_update_layout(subtitle_label_);
        lv_obj_invalidate(subtitle_label_);
    }
    lv_refr_now(lv_obj_get_display(subtitle_label_));
    return true;

}

bool StackChanBoard::EnsureRouteBadge() {

    if (route_badge_ != nullptr) {
        return true;
    }
    lv_obj_t* screen = lv_screen_active();
    if (screen == nullptr) {
        return false;
    }
    route_badge_ = lv_label_create(screen);
    if (route_badge_ == nullptr) {
        return false;
    }
    // Same translucent black backing as status_label_ for legibility.
    lv_obj_set_style_bg_color(route_badge_, lv_color_black(), 0);
    lv_obj_set_style_bg_opa(route_badge_, LV_OPA_60, 0);
    lv_obj_set_style_text_color(route_badge_, lv_color_white(), 0);
    lv_obj_set_style_radius(route_badge_, 8, 0);
    lv_obj_set_style_pad_left(route_badge_, 6, 0);
    lv_obj_set_style_pad_right(route_badge_, 6, 0);
    lv_obj_set_style_pad_top(route_badge_, 3, 0);
    lv_obj_set_style_pad_bottom(route_badge_, 3, 0);
    // Top-right corner so it never overlaps status_label_ (top-centre).
    lv_obj_align(route_badge_, LV_ALIGN_TOP_RIGHT, -4, 6);
    lv_obj_clear_flag(route_badge_, LV_OBJ_FLAG_SCROLLABLE);
    // Hidden until the first non-empty SetRouteBadge().
    lv_obj_add_flag(route_badge_, LV_OBJ_FLAG_HIDDEN);
    lv_obj_move_foreground(route_badge_);
    ESP_LOGI(TAG, "Route badge created on active screen");
    return true;

}

bool StackChanBoard::SetRouteBadge(const char* text) {

    if (display_ == nullptr) {
        ESP_LOGW(TAG, "SetRouteBadge ignored: display_ not ready");
        return false;
    }
    const char* safe = (text != nullptr) ? text : "";
    DisplayLockGuard lock(display_);
    if (!EnsureRouteBadge()) {
        return false;
    }
    if (safe[0] == '\0') {
        lv_obj_add_flag(route_badge_, LV_OBJ_FLAG_HIDDEN);
        // Redraw the vacated area (the hidden label can't invalidate
        // itself) so the badge clears immediately. See SetStatusText().
        lv_obj_invalidate(lv_obj_get_parent(route_badge_));
    } else {
        lv_label_set_text(route_badge_, safe);
        lv_obj_clear_flag(route_badge_, LV_OBJ_FLAG_HIDDEN);
        lv_obj_move_foreground(route_badge_);
        // Same-frame flush so the badge appears without a one-turn lag.
        lv_obj_update_layout(route_badge_);
        lv_obj_invalidate(route_badge_);
    }
    lv_refr_now(lv_obj_get_display(route_badge_));
    return true;

}

void StackChanBoard::BlinkStepCb(void* arg) {

    StackChanBoard* self = static_cast<StackChanBoard*>(arg);
    self->BlinkStepAdvance();

}

void StackChanBoard::BlinkStepAdvance() {

    if (display_ == nullptr) {
        blink_state_ = BlinkState::IDLE;
        return;
    }
    DisplayLockGuard lock(display_);
    switch (blink_state_) {
        case BlinkState::EYES_HALF_DOWN:
            current_eyes_index_ = 2;  // closed
            active_layer_ = ActiveLayer::EYES;
            RenderAvatarLocked();
            blink_state_ = BlinkState::EYES_CLOSED;
            esp_timer_start_once(blink_step_timer_, BLINK_STEP_MS * 1000);
            break;
        case BlinkState::EYES_CLOSED:
            current_eyes_index_ = 1;  // half
            active_layer_ = ActiveLayer::EYES;
            RenderAvatarLocked();
            blink_state_ = BlinkState::EYES_HALF_UP;
            esp_timer_start_once(blink_step_timer_, BLINK_STEP_MS * 1000);
            break;
        case BlinkState::EYES_HALF_UP:
            // Final: restore the resting state. In layered mode this
            // repaints the face image (the Phase 2 trade-off: any
            // active mouth overlay is replaced by the face). In matrix
            // mode the mouth index is preserved so the composite frame
            // keeps the user's lip-sync state.
            RestoreCurrentFaceLocked();
            blink_state_ = BlinkState::IDLE;
            break;
        case BlinkState::IDLE:
        default:
            // Stale callback; nothing to do.
            break;
    }

}

void StackChanBoard::BlinkScheduleCb(void* arg) {

    StackChanBoard* self = static_cast<StackChanBoard*>(arg);
    self->BlinkScheduleTick();

}

void StackChanBoard::BlinkScheduleTick() {

    if (blink_enabled_ && blink_state_ == BlinkState::IDLE && display_ != nullptr) {
        // Begin the blink: half-down now, full-closed at next step.
        DisplayLockGuard lock(display_);
        current_eyes_index_ = 1;  // half
        active_layer_ = ActiveLayer::EYES;
        if (RenderAvatarLocked()) {
            blink_state_ = BlinkState::EYES_HALF_DOWN;
            esp_timer_start_once(blink_step_timer_, BLINK_STEP_MS * 1000);
        }
    }
    // Re-arm scheduler with a fresh random interval, even if we skipped
    // this blink (e.g. avatar not yet on screen). This keeps the cadence
    // organic instead of clumping after a long pause.
    if (blink_enabled_) {
        uint32_t span_ms = BLINK_MAX_GAP_MS - BLINK_MIN_GAP_MS;
        uint32_t next_ms = BLINK_MIN_GAP_MS + (esp_random() % span_ms);
        esp_timer_start_once(blink_schedule_timer_, (uint64_t)next_ms * 1000);
    }

}

void StackChanBoard::EnsureBlinkTimers() {

    if (blink_step_timer_ == nullptr) {
        esp_timer_create_args_t step_args = {
            .callback = &StackChanBoard::BlinkStepCb,
            .arg = this,
            .dispatch_method = ESP_TIMER_TASK,
            .name = "blink_step",
            .skip_unhandled_events = true,
        };
        ESP_ERROR_CHECK(esp_timer_create(&step_args, &blink_step_timer_));
    }
    if (blink_schedule_timer_ == nullptr) {
        esp_timer_create_args_t sched_args = {
            .callback = &StackChanBoard::BlinkScheduleCb,
            .arg = this,
            .dispatch_method = ESP_TIMER_TASK,
            .name = "blink_sched",
            .skip_unhandled_events = true,
        };
        ESP_ERROR_CHECK(esp_timer_create(&sched_args, &blink_schedule_timer_));
    }

}

void StackChanBoard::StartBlinkTimer() {

    EnsureBlinkTimers();
    blink_enabled_ = true;
    // Make sure no leftover schedule timer is running, then arm one with
    // a fresh random interval.
    esp_timer_stop(blink_schedule_timer_);
    uint32_t span_ms = BLINK_MAX_GAP_MS - BLINK_MIN_GAP_MS;
    uint32_t first_ms = BLINK_MIN_GAP_MS + (esp_random() % span_ms);
    esp_timer_start_once(blink_schedule_timer_, (uint64_t)first_ms * 1000);
    ESP_LOGI(TAG, "Blink ENABLED (first blink in %u ms)", (unsigned)first_ms);

}

void StackChanBoard::StopBlinkTimer() {

    blink_enabled_ = false;
    if (blink_schedule_timer_ != nullptr) {
        esp_timer_stop(blink_schedule_timer_);
    }
    if (blink_step_timer_ != nullptr) {
        esp_timer_stop(blink_step_timer_);
    }
    // If we stopped mid-sequence, snap back to the resting face so the
    // user is not left staring at half-closed eyes.
    if (blink_state_ != BlinkState::IDLE && display_ != nullptr) {
        DisplayLockGuard lock(display_);
        RestoreCurrentFaceLocked();
    }
    blink_state_ = BlinkState::IDLE;
    ESP_LOGI(TAG, "Blink DISABLED");

}

void StackChanBoard::TtsLipSyncStepCb(void* arg) {

    static_cast<StackChanBoard*>(arg)->TtsLipSyncStepAdvance();

}

void StackChanBoard::TtsLipSyncStepAdvance() {

    if (!tts_lipsync_active_.load(std::memory_order_acquire)) {
        return;
    }
    if (display_ == nullptr) {
        return;
    }
    // Yield to an in-flight user-issued mouth sequence; re-arm so we
    // resume on our cadence as soon as the sequence finishes.
    if (mouth_seq_active_.load(std::memory_order_acquire)) {
        esp_timer_start_once(tts_lipsync_timer_,
                             (uint64_t)TTS_LIPSYNC_STEP_MS * 1000);
        return;
    }
    const char* shape = nullptr;
    switch (tts_lipsync_shape_) {
        case TtsLipSyncShape::CLOSED:
            shape = "half";
            tts_lipsync_shape_ = TtsLipSyncShape::HALF_RISING;
            break;
        case TtsLipSyncShape::HALF_RISING:
            shape = "open";
            tts_lipsync_shape_ = TtsLipSyncShape::OPEN;
            break;
        case TtsLipSyncShape::OPEN:
            shape = "half";
            tts_lipsync_shape_ = TtsLipSyncShape::HALF_FALLING;
            break;
        case TtsLipSyncShape::HALF_FALLING:
        default:
            shape = "closed";
            tts_lipsync_shape_ = TtsLipSyncShape::CLOSED;
            break;
    }
    SetMouthShape(shape);
    if (tts_lipsync_active_.load(std::memory_order_acquire)) {
        esp_timer_start_once(tts_lipsync_timer_,
                             (uint64_t)TTS_LIPSYNC_STEP_MS * 1000);
    }

}

void StackChanBoard::EnsureTtsLipSyncTimer() {

    if (tts_lipsync_timer_ == nullptr) {
        esp_timer_create_args_t args = {
            .callback = &StackChanBoard::TtsLipSyncStepCb,
            .arg = this,
            .dispatch_method = ESP_TIMER_TASK,
            .name = "tts_lipsync",
            .skip_unhandled_events = true,
        };
        ESP_ERROR_CHECK(esp_timer_create(&args, &tts_lipsync_timer_));
    }

}

void StackChanBoard::StartTtsLipSync() {

    if (display_ == nullptr) {
        ESP_LOGD(TAG, "StartTtsLipSync ignored: display_ not ready");
        return;
    }
    EnsureTtsLipSyncTimer();
    if (tts_lipsync_active_.exchange(true, std::memory_order_acq_rel)) {
        // Already active (e.g. duplicate tts.start); nothing to do.
        return;
    }
    // Pause autonomous blink so BlinkStepAdvance()'s
    // RestoreCurrentFaceLocked() does not overwrite the mouth overlay.
    // blink_desired_ remembers the user's intent for restore at stop.
    StopBlinkTimer();
    // Start the cycle from a known resting position so the first audible
    // frame opens the mouth from closed.
    tts_lipsync_shape_ = TtsLipSyncShape::CLOSED;
    SetMouthShape("closed");
    esp_timer_start_once(tts_lipsync_timer_,
                         (uint64_t)TTS_LIPSYNC_STEP_MS * 1000);
    ESP_LOGI(TAG, "TTS lip-sync STARTED (cycle=%d ms)",
             TTS_LIPSYNC_STEP_MS);

}

void StackChanBoard::StopTtsLipSync() {

    if (!tts_lipsync_active_.exchange(false, std::memory_order_acq_rel)) {
        return;  // already stopped
    }
    if (tts_lipsync_timer_ != nullptr) {
        esp_timer_stop(tts_lipsync_timer_);
    }
    // If a user-issued mouth sequence is in flight (we were yielding
    // our frames to it via the mouth_seq_active_ guard in
    // TtsLipSyncStepAdvance), let the sequence task own both the
    // mouth shape and the blink restore at sequence end. Touching
    // either here would race the sequence:
    //   - SetMouthShape("closed") would clobber the user's current
    //     frame mid-sequence;
    //   - StartBlinkTimer() would let BlinkStepAdvance()'s
    //     RestoreCurrentFaceLocked() overwrite the mouth overlay
    //     before the sequence finishes drawing (Phase 2 trade-off).
    // The sequence task already restores blink from blink_desired_
    // at its own end (see MouthSequenceTaskLoop), so deferring is
    // safe and idempotent.
    if (mouth_seq_active_.load(std::memory_order_acquire)) {
        ESP_LOGI(TAG,
                 "TTS lip-sync STOPPED (mouth_seq active; deferring "
                 "mouth + blink restore to sequence end)");
        return;
    }
    // Snap back to a closed mouth so the device does not freeze on a
    // half-open frame.
    if (display_ != nullptr) {
        SetMouthShape("closed");
    }
    // Restore blink based on the user's most recent intent (mirrors the
    // mouth-sequence playback task's restore semantics).
    if (blink_desired_.load(std::memory_order_acquire)) {
        StartBlinkTimer();
    }
    ESP_LOGI(TAG, "TTS lip-sync STOPPED");

}

void StackChanBoard::RequestMouthSequenceCancel() {

    if (mouth_seq_lock_ == nullptr) {
        return;
    }
    if (xSemaphoreTake(mouth_seq_lock_, portMAX_DELAY) == pdTRUE) {
        mouth_seq_pending_.clear();
        mouth_seq_cancel_requested_.store(true, std::memory_order_release);
        mouth_seq_generation_.fetch_add(1, std::memory_order_release);
        xSemaphoreGive(mouth_seq_lock_);
    }

}

StackChanBoard::MouthSequenceEnqueueResult StackChanBoard::EnqueueMouthSequence(const std::string& steps_json) {

    MouthSequenceEnqueueResult r{false, std::string(), 0, 0};

    cJSON* root = cJSON_Parse(steps_json.c_str());
    if (root == nullptr) {
        r.error = "steps must be a JSON array (parse failed)";
        return r;
    }
    if (!cJSON_IsArray(root)) {
        r.error = "steps must be a JSON array";
        cJSON_Delete(root);
        return r;
    }
    int n = cJSON_GetArraySize(root);
    if (n < 1 || n > kMaxMouthSequenceSteps) {
        r.error = std::string("steps length out of range (1..") +
                  std::to_string(kMaxMouthSequenceSteps) + ")";
        cJSON_Delete(root);
        return r;
    }

    std::vector<MouthStep> parsed;
    parsed.reserve(static_cast<size_t>(n));
    uint32_t total = 0;
    for (int i = 0; i < n; ++i) {
        cJSON* item = cJSON_GetArrayItem(root, i);
        if (!cJSON_IsObject(item)) {
            r.error = std::string("step[") + std::to_string(i) + "] must be an object";
            cJSON_Delete(root);
            return r;
        }
        cJSON* shape = cJSON_GetObjectItem(item, "shape");
        cJSON* dur = cJSON_GetObjectItem(item, "duration_ms");
        if (!cJSON_IsString(shape) || shape->valuestring == nullptr) {
            r.error = std::string("step[") + std::to_string(i) + "].shape must be a string";
            cJSON_Delete(root);
            return r;
        }
        if (!cJSON_IsNumber(dur)) {
            r.error = std::string("step[") + std::to_string(i) + "].duration_ms must be an integer";
            cJSON_Delete(root);
            return r;
        }
        if (MouthShapeToIndex(shape->valuestring) < 0) {
            r.error = std::string("step[") + std::to_string(i) +
                      "].shape unknown: '" + shape->valuestring +
                      "' (allowed: closed, half, open, e, u)";
            cJSON_Delete(root);
            return r;
        }
        int d = dur->valueint;
        if (d < kMouthStepMinMs || d > kMouthStepMaxMs) {
            r.error = std::string("step[") + std::to_string(i) +
                      "].duration_ms out of range (" +
                      std::to_string(kMouthStepMinMs) + ".." +
                      std::to_string(kMouthStepMaxMs) + ")";
            cJSON_Delete(root);
            return r;
        }
        parsed.push_back({std::string(shape->valuestring),
                          static_cast<uint32_t>(d)});
        total += static_cast<uint32_t>(d);
    }
    cJSON_Delete(root);

    if (mouth_seq_lock_ == nullptr || mouth_seq_signal_ == nullptr ||
        mouth_seq_task_ == nullptr) {
        r.error = "mouth sequence task not initialised";
        return r;
    }

    // Atomically replace the pending queue and mark any in-flight
    // sequence for cancellation so it stops at the next slice. The
    // generation bump is what makes a fresh enqueue preempt the
    // currently-playing sequence even between cancel-flag checks
    // and SetMouthShape() calls (per-step generation re-check in
    // MouthSequenceTaskLoop).
    if (xSemaphoreTake(mouth_seq_lock_, portMAX_DELAY) == pdTRUE) {
        mouth_seq_pending_ = std::move(parsed);
        if (mouth_seq_active_.load(std::memory_order_acquire)) {
            mouth_seq_cancel_requested_.store(true, std::memory_order_release);
        }
        mouth_seq_generation_.fetch_add(1, std::memory_order_release);
        xSemaphoreGive(mouth_seq_lock_);
    }
    // Wake the task. If the task is already running through a previous
    // sequence, it will pick up the new pending queue after observing
    // cancel_requested at the next slice and looping back.
    xSemaphoreGive(mouth_seq_signal_);

    r.ok = true;
    r.queued_steps = n;
    r.total_duration_ms = total;
    return r;

}

void StackChanBoard::MouthSequenceTaskTrampoline(void* arg) {

    static_cast<StackChanBoard*>(arg)->MouthSequenceTaskLoop();

}

void StackChanBoard::MouthSequenceTaskLoop() {

    for (;;) {
        // Wait until something is enqueued (or self-signaled at the
        // tail of a previous run when more pending was discovered).
        xSemaphoreTake(mouth_seq_signal_, portMAX_DELAY);

        // Drain whatever is pending right now into a local copy so
        // we can release the lock before walking the sequence. Latch
        // the generation under the same lock so we can reject any
        // newer preempt at the next per-step check.
        std::vector<MouthStep> seq;
        uint32_t my_generation = 0;
        if (xSemaphoreTake(mouth_seq_lock_, portMAX_DELAY) == pdTRUE) {
            seq = std::move(mouth_seq_pending_);
            mouth_seq_pending_.clear();
            mouth_seq_cancel_requested_.store(false, std::memory_order_release);
            mouth_seq_active_.store(!seq.empty(), std::memory_order_release);
            my_generation = mouth_seq_generation_.load(std::memory_order_acquire);
            xSemaphoreGive(mouth_seq_lock_);
        }
        if (seq.empty()) {
            continue;
        }

        // Pause autonomous blink for the duration of the sequence so
        // BlinkStepAdvance()'s RestoreCurrentFaceLocked() does not
        // overwrite the active mouth overlay. Note: we no longer
        // snapshot blink_enabled_ here — the user's intent is read
        // from blink_desired_ at sequence end so calls to set_blink
        // made during playback are honoured.
        StopBlinkTimer();

        for (const auto& step : seq) {
            // Re-check cancel + generation right before each frame
            // draw. Any preempt issued between this check and the
            // previous SetMouthShape() will be observed here, so we
            // never draw a frame after a newer set_mouth / set_avatar
            // / set_mouth_sequence handler has returned to the caller.
            if (mouth_seq_cancel_requested_.load(std::memory_order_acquire) ||
                mouth_seq_generation_.load(std::memory_order_acquire) != my_generation) {
                break;
            }
            SetMouthShape(step.shape.c_str());
            // Sleep in small slices so cancel is observed quickly.
            uint32_t remaining = step.duration_ms;
            while (remaining > 0 &&
                   !mouth_seq_cancel_requested_.load(std::memory_order_acquire) &&
                   mouth_seq_generation_.load(std::memory_order_acquire) == my_generation) {
                uint32_t slice = remaining > kMouthCancelSliceMs
                                     ? kMouthCancelSliceMs
                                     : remaining;
                vTaskDelay(pdMS_TO_TICKS(slice));
                remaining -= slice;
            }
        }

        // Restore blink according to the user's most recent intent,
        // not a snapshot taken before the sequence started. This way
        // a set_blink(true/false) issued during the sequence is the
        // one that wins at the end.
        if (blink_desired_.load(std::memory_order_acquire)) {
            StartBlinkTimer();
        }

        // If a fresh sequence was enqueued during playback, the
        // cancel path above will have left it in mouth_seq_pending_.
        // Self-signal so the next outer-loop iteration picks it up
        // immediately rather than parking on the semaphore.
        bool has_more = false;
        if (xSemaphoreTake(mouth_seq_lock_, portMAX_DELAY) == pdTRUE) {
            mouth_seq_active_.store(false, std::memory_order_release);
            has_more = !mouth_seq_pending_.empty();
            xSemaphoreGive(mouth_seq_lock_);
        }
        if (has_more) {
            xSemaphoreGive(mouth_seq_signal_);
        }
    }

}

void StackChanBoard::InitializeMouthSequenceTask() {

    if (mouth_seq_task_ != nullptr) {
        return;
    }
    mouth_seq_lock_ = xSemaphoreCreateMutex();
    mouth_seq_signal_ = xSemaphoreCreateBinary();
    if (mouth_seq_lock_ == nullptr || mouth_seq_signal_ == nullptr) {
        ESP_LOGE(TAG, "Failed to create mouth sequence sync primitives");
        if (mouth_seq_lock_ != nullptr) {
            vSemaphoreDelete(mouth_seq_lock_);
            mouth_seq_lock_ = nullptr;
        }
        if (mouth_seq_signal_ != nullptr) {
            vSemaphoreDelete(mouth_seq_signal_);
            mouth_seq_signal_ = nullptr;
        }
        return;
    }
    BaseType_t ok = xTaskCreate(&StackChanBoard::MouthSequenceTaskTrampoline,
                                "mouth_seq", 4096, this,
                                tskIDLE_PRIORITY + 2,
                                &mouth_seq_task_);
    if (ok != pdPASS) {
        ESP_LOGE(TAG, "Failed to create mouth_seq task");
        vSemaphoreDelete(mouth_seq_lock_);
        mouth_seq_lock_ = nullptr;
        vSemaphoreDelete(mouth_seq_signal_);
        mouth_seq_signal_ = nullptr;
        mouth_seq_task_ = nullptr;
    } else {
        ESP_LOGI(TAG, "Mouth sequence task ready");
    }

}

void StackChanBoard::OnAvatarSetFetch(const cJSON* root) {

    if (root == nullptr) {
        ESP_LOGW(TAG, "OnAvatarSetFetch: root is null");
        return;
    }
    auto url      = cJSON_GetObjectItem(root, "url");
    auto token    = cJSON_GetObjectItem(root, "token");
    auto mode_j   = cJSON_GetObjectItem(root, "mode");
    auto checksum = cJSON_GetObjectItem(root, "checksum");
    auto size_j   = cJSON_GetObjectItem(root, "expected_size");

    // The gateway correlates avatar_set_loaded replies by checksum
    // (see ESP32Connection._avatar_set_waiters). Reply with the
    // requested checksum on every error path so a failure can wake
    // the waiter promptly instead of timing out.
    const std::string req_checksum =
        cJSON_IsString(checksum) ? checksum->valuestring : "";

    if (!cJSON_IsString(url) || !cJSON_IsString(token) ||
        !cJSON_IsString(mode_j) || !cJSON_IsNumber(size_j)) {
        ESP_LOGW(TAG, "OnAvatarSetFetch: missing required fields");
        SendAvatarSetLoadedError(req_checksum, "missing_fields");
        return;
    }

    AvatarSet::Mode mode_enum;
    if (strcmp(mode_j->valuestring, "layered") == 0) {
        mode_enum = AvatarSet::Mode::kLayered;
    } else if (strcmp(mode_j->valuestring, "matrix") == 0) {
        mode_enum = AvatarSet::Mode::kMatrix;
    } else {
        ESP_LOGW(TAG, "OnAvatarSetFetch: unknown mode '%s'", mode_j->valuestring);
        SendAvatarSetLoadedError(req_checksum, "unknown_mode");
        return;
    }

    // Take the in-progress guard. exchange(true) returns the previous
    // value, so if another fetch was already running we reject this
    // request rather than racing on avatar_set_'s PSRAM swap. The
    // pending lock is created lazily (the defer helpers do the same;
    // create it here so both producer and consumer share the same
    // mutex instance).
    if (avatar_fetch_in_progress_.exchange(true, std::memory_order_acq_rel)) {
        ESP_LOGW(TAG, "OnAvatarSetFetch: another fetch already in progress");
        SendAvatarSetLoadedError(req_checksum, "fetch_in_progress");
        return;
    }
    EnsureAvatarPendingLock();
    if (avatar_pending_lock_ != nullptr &&
        xSemaphoreTake(avatar_pending_lock_, portMAX_DELAY) == pdTRUE) {
        avatar_pending_ = PendingAvatarState{};
        xSemaphoreGive(avatar_pending_lock_);
    }
    // Quiesce every autonomous LVGL writer so no set_src lands while
    // AvatarSet::AdoptOwnedBuffer atomically swaps the PSRAM buffer backing
    // each lv_image_dsc_t. The schedule timers / state machines restart
    // from ApplyPendingAvatarAfterFetch (blink) or the next tts.start
    // (TTS lipsync) once the fetch resolves.
    StopTtsLipSync();
    RequestMouthSequenceCancel();
    StopBlinkTimer();

    auto* context = new AvatarFetchContext;
    context->board = this;
    context->url = url->valuestring;
    context->token = token->valuestring;
    context->mode = mode_enum;
    context->expected_size = static_cast<size_t>(size_j->valuedouble);
    context->expected_sha256 = cJSON_IsString(checksum) ? checksum->valuestring : "";

    BaseType_t ok = xTaskCreate(
        &StackChanBoard::AvatarFetchTaskTrampoline,
        "avatar_fetch",
        8192,
        context,
        tskIDLE_PRIORITY + 2,
        nullptr);
    if (ok != pdPASS) {
        ESP_LOGE(TAG, "OnAvatarSetFetch: failed to create avatar_fetch task");
        delete context;
        avatar_fetch_in_progress_.store(false, std::memory_order_release);
        SendAvatarSetLoadedError(req_checksum, "task_create_failed");
    }

}

void StackChanBoard::AvatarFetchTaskTrampoline(void* arg) {

    auto* ctx = static_cast<AvatarFetchContext*>(arg);
    ctx->board->RunAvatarFetch(ctx);
    delete ctx;
    vTaskDelete(nullptr);

}

void StackChanBoard::RunAvatarFetch(const AvatarFetchContext* ctx) {

    // Capture expected_sha256 by value so the callback can fall back
    // to it when AvatarSetFetcher reports an error before computing
    // the actual checksum (HTTP error, size mismatch, allocation
    // failure, etc.). The gateway's _avatar_set_waiters dict is keyed
    // by checksum; replying with an empty key means the failure
    // cannot resolve any waiter and the caller waits until timeout.
    const std::string expected_sha256 = ctx->expected_sha256;
    AvatarSetFetcher::Fetch(
        avatar_set_,
        ctx->url, ctx->token,
        ctx->mode, ctx->expected_size, ctx->expected_sha256,
        [expected_sha256](bool ok,
                          const std::string& actual_checksum,
                          const std::string& error_code) {
            const std::string& correlation =
                actual_checksum.empty() ? expected_sha256 : actual_checksum;
            SendAvatarSetLoaded(ok, correlation, error_code);
        });

    // Fetch finished (success or failure). Clear the in-progress flag
    // BEFORE replaying pending state — otherwise the public
    // SetAvatarExpression / SetMouthShape / StartBlinkTimer calls
    // inside ApplyPendingAvatarAfterFetch would loop back into the
    // defer helpers and the pending state would never be drained.
    avatar_fetch_in_progress_.store(false, std::memory_order_release);

    // After a successful adoption the previously displayed face is still
    // pointing into the freed static-table data via avatar_img_; force a
    // refresh so the new AvatarSet entry is picked up by the next
    // RenderAvatarLocked() call (driven by SetAvatarExpressionIfActive
    // below). Skipped on failure (the old image is still valid).
    if (avatar_set_.is_loaded()) {
        SetAvatarExpressionIfActive(current_avatar_face_.c_str());
    }

    // Replay the latest face / mouth / blink intent the user expressed
    // while the fetch was running. Order: "off" wins over a face if
    // both were issued (mutually exclusive on the face axis); blink
    // restoration happens last so a successful fetch doesn't restart
    // blink if the user disabled it mid-fetch.
    ApplyPendingAvatarAfterFetch();

}

void StackChanBoard::SendAvatarSetLoaded(bool ok, const std::string& checksum, const std::string& error_code) {

    cJSON* root = cJSON_CreateObject();
    if (root == nullptr) return;
    cJSON_AddStringToObject(root, "type", "avatar_set_loaded");
    cJSON_AddStringToObject(root, "checksum", checksum.c_str());
    cJSON_AddBoolToObject(root, "ok", ok);
    if (ok || error_code.empty()) {
        cJSON_AddNullToObject(root, "error");
    } else {
        cJSON_AddStringToObject(root, "error", error_code.c_str());
    }
    char* str = cJSON_PrintUnformatted(root);
    if (str != nullptr) {
        Application::GetInstance().SendJsonString(std::string(str));
        cJSON_free(str);
    }
    cJSON_Delete(root);

}

void StackChanBoard::SendAvatarSetLoadedError(const std::string& checksum, const std::string& error_code) {

    SendAvatarSetLoaded(false, checksum, error_code);

}
