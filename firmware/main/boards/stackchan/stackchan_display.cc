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

void StackChanBoard::InitializeSpi() {

    spi_bus_config_t buscfg = {};
    buscfg.mosi_io_num = GPIO_NUM_37;
    buscfg.miso_io_num = GPIO_NUM_NC;
    buscfg.sclk_io_num = GPIO_NUM_36;
    buscfg.quadwp_io_num = GPIO_NUM_NC;
    buscfg.quadhd_io_num = GPIO_NUM_NC;
    buscfg.max_transfer_sz = DISPLAY_WIDTH * DISPLAY_HEIGHT * sizeof(uint16_t);
    ESP_ERROR_CHECK(spi_bus_initialize(SPI3_HOST, &buscfg, SPI_DMA_CH_AUTO));

}

void StackChanBoard::InitializeIli9342Display() {

    ESP_LOGI(TAG, "Init IlI9342");

    esp_lcd_panel_io_handle_t panel_io = nullptr;
    esp_lcd_panel_handle_t panel = nullptr;

    ESP_LOGD(TAG, "Install panel IO");
    esp_lcd_panel_io_spi_config_t io_config = {};
    io_config.cs_gpio_num = GPIO_NUM_3;
    io_config.dc_gpio_num = GPIO_NUM_35;
    io_config.spi_mode = 2;
    io_config.pclk_hz = 40 * 1000 * 1000;
    io_config.trans_queue_depth = 10;
    io_config.lcd_cmd_bits = 8;
    io_config.lcd_param_bits = 8;
    ESP_ERROR_CHECK(esp_lcd_new_panel_io_spi(SPI3_HOST, &io_config, &panel_io));

    ESP_LOGD(TAG, "Install LCD driver");
    esp_lcd_panel_dev_config_t panel_config = {};
    panel_config.reset_gpio_num = GPIO_NUM_NC;
    panel_config.rgb_ele_order = LCD_RGB_ELEMENT_ORDER_BGR;
    panel_config.bits_per_pixel = 16;
    ESP_ERROR_CHECK(esp_lcd_new_panel_ili9341(panel_io, &panel_config, &panel));
    
    esp_lcd_panel_reset(panel);
    aw9523_->ResetIli9342();

    esp_lcd_panel_init(panel);
    esp_lcd_panel_invert_color(panel, true);
    esp_lcd_panel_swap_xy(panel, DISPLAY_SWAP_XY);
    esp_lcd_panel_mirror(panel, DISPLAY_MIRROR_X, DISPLAY_MIRROR_Y);

    display_ = new SpiLcdDisplay(panel_io, panel,
                                DISPLAY_WIDTH, DISPLAY_HEIGHT, DISPLAY_OFFSET_X, DISPLAY_OFFSET_Y, DISPLAY_MIRROR_X, DISPLAY_MIRROR_Y, DISPLAY_SWAP_XY);

}

int StackChanBoard::FaceNameToIndex(const char* face) {

    if (face == nullptr) return -1;
    if (strcmp(face, "idle") == 0)        return 0;
    if (strcmp(face, "happy") == 0)       return 1;
    if (strcmp(face, "thinking") == 0)    return 2;
    if (strcmp(face, "sad") == 0)         return 3;
    if (strcmp(face, "surprised") == 0)   return 4;
    if (strcmp(face, "embarrassed") == 0) return 5;
    return -1;

}

int StackChanBoard::MouthShapeToIndex(const char* shape) {

    if (shape == nullptr) return -1;
    if (strcmp(shape, "closed") == 0) return 0;
    if (strcmp(shape, "half") == 0)   return 1;
    if (strcmp(shape, "open") == 0)   return 2;
    if (strcmp(shape, "e") == 0)      return 3;
    if (strcmp(shape, "u") == 0)      return 4;
    return -1;

}

const lv_image_dsc_t* StackChanBoard::FaceImageForIndex(int face_index) const {

    if (avatar_set_.is_loaded() &&
        avatar_set_.mode() == AvatarSet::Mode::kLayered) {
        const lv_image_dsc_t* dsc = avatar_set_.GetFace(face_index);
        if (dsc != nullptr) return dsc;
    }
    switch (face_index) {
        case 0: return &avatar_idle;
        case 1: return &avatar_happy;
        case 2: return &avatar_thinking;
        case 3: return &avatar_sad;
        case 4: return &avatar_surprised;
        case 5: return &avatar_embarrassed;
        default: return nullptr;
    }

}

const lv_image_dsc_t* StackChanBoard::EyesImageForIndex(int eyes_index) const {

    if (avatar_set_.is_loaded() &&
        avatar_set_.mode() == AvatarSet::Mode::kLayered) {
        const lv_image_dsc_t* dsc = avatar_set_.GetEyes(eyes_index);
        if (dsc != nullptr) return dsc;
    }
    switch (eyes_index) {
        case 0: return &avatar_eyes_open;
        case 1: return &avatar_eyes_half;
        case 2: return &avatar_eyes_closed;
        default: return nullptr;
    }

}

const lv_image_dsc_t* StackChanBoard::MouthImageForIndex(int mouth_index) const {

    if (avatar_set_.is_loaded() &&
        avatar_set_.mode() == AvatarSet::Mode::kLayered) {
        const lv_image_dsc_t* dsc = avatar_set_.GetMouth(mouth_index);
        if (dsc != nullptr) return dsc;
    }
    switch (mouth_index) {
        case 0: return &avatar_mouth_closed;
        case 1: return &avatar_mouth_half;
        case 2: return &avatar_mouth_open;
        case 3: return &avatar_mouth_e;
        case 4: return &avatar_mouth_u;
        default: return nullptr;
    }

}

bool StackChanBoard::RenderAvatarLocked() {

    const lv_image_dsc_t* dsc = nullptr;
    if (avatar_set_.is_loaded() &&
        avatar_set_.mode() == AvatarSet::Mode::kMatrix) {
        dsc = avatar_set_.GetMatrix(current_face_index_,
                                    current_eyes_index_,
                                    current_mouth_index_);
    } else {
        switch (active_layer_) {
            case ActiveLayer::FACE:
                dsc = FaceImageForIndex(current_face_index_);
                break;
            case ActiveLayer::EYES:
                dsc = EyesImageForIndex(current_eyes_index_);
                break;
            case ActiveLayer::MOUTH:
                dsc = MouthImageForIndex(current_mouth_index_);
                break;
        }
    }
    if (dsc == nullptr) return false;
    if (!EnsureAvatarObject()) return false;
    lv_image_set_src(avatar_img_, dsc);
    lv_obj_move_foreground(avatar_img_);
    // The text overlays are siblings of the full-screen avatar. Each
    // avatar repaint (blink, lip-sync, face change) raises avatar_img_ to
    // the front, which would otherwise bury any visible overlay. Re-
    // promote each one here so it stays on top while shown.
    PromoteOverlaysLocked();
    return true;

}

void StackChanBoard::PromoteOverlaysLocked() {

    if (status_label_ != nullptr &&
        !lv_obj_has_flag(status_label_, LV_OBJ_FLAG_HIDDEN)) {
        lv_obj_move_foreground(status_label_);
    }
    if (subtitle_label_ != nullptr &&
        !lv_obj_has_flag(subtitle_label_, LV_OBJ_FLAG_HIDDEN)) {
        lv_obj_move_foreground(subtitle_label_);
    }
    if (route_badge_ != nullptr &&
        !lv_obj_has_flag(route_badge_, LV_OBJ_FLAG_HIDDEN)) {
        lv_obj_move_foreground(route_badge_);
    }

}

void StackChanBoard::EnsureAvatarPendingLock() {

    if (avatar_pending_lock_ == nullptr) {
        avatar_pending_lock_ = xSemaphoreCreateMutex();
    }

}

bool StackChanBoard::DeferAvatarFaceIfFetching(const char* face) {

    if (!avatar_fetch_in_progress_.load(std::memory_order_acquire)) return false;
    EnsureAvatarPendingLock();
    if (avatar_pending_lock_ == nullptr) return false;
    if (xSemaphoreTake(avatar_pending_lock_, portMAX_DELAY) == pdTRUE) {
        avatar_pending_.has_off = false;
        avatar_pending_.has_face = true;
        avatar_pending_.face_name = (face != nullptr) ? face : "";
        xSemaphoreGive(avatar_pending_lock_);
    }
    ESP_LOGI(TAG, "SetAvatarExpression('%s') deferred (avatar fetch in progress)",
             face != nullptr ? face : "(null)");
    return true;

}

bool StackChanBoard::DeferAvatarOffIfFetching() {

    if (!avatar_fetch_in_progress_.load(std::memory_order_acquire)) return false;
    EnsureAvatarPendingLock();
    if (avatar_pending_lock_ == nullptr) return false;
    if (xSemaphoreTake(avatar_pending_lock_, portMAX_DELAY) == pdTRUE) {
        avatar_pending_.has_face = false;
        avatar_pending_.face_name.clear();
        avatar_pending_.has_off = true;
        xSemaphoreGive(avatar_pending_lock_);
    }
    ESP_LOGI(TAG, "SetAvatarOff deferred (avatar fetch in progress)");
    return true;

}

bool StackChanBoard::DeferAvatarMouthIfFetching(const char* shape) {

    if (!avatar_fetch_in_progress_.load(std::memory_order_acquire)) return false;
    EnsureAvatarPendingLock();
    if (avatar_pending_lock_ == nullptr) return false;
    if (xSemaphoreTake(avatar_pending_lock_, portMAX_DELAY) == pdTRUE) {
        avatar_pending_.has_mouth = true;
        avatar_pending_.mouth_shape = (shape != nullptr) ? shape : "";
        xSemaphoreGive(avatar_pending_lock_);
    }
    ESP_LOGI(TAG, "SetMouthShape('%s') deferred (avatar fetch in progress)",
             shape != nullptr ? shape : "(null)");
    return true;

}

bool StackChanBoard::DeferAvatarBlinkIfFetching(bool enabled) {

    if (!avatar_fetch_in_progress_.load(std::memory_order_acquire)) return false;
    EnsureAvatarPendingLock();
    if (avatar_pending_lock_ == nullptr) return false;
    if (xSemaphoreTake(avatar_pending_lock_, portMAX_DELAY) == pdTRUE) {
        avatar_pending_.has_blink = true;
        avatar_pending_.blink_enabled = enabled;
        xSemaphoreGive(avatar_pending_lock_);
    }
    ESP_LOGI(TAG, "set_blink(%d) deferred (avatar fetch in progress)", (int)enabled);
    return true;

}

void StackChanBoard::ApplyPendingAvatarAfterFetch() {

    PendingAvatarState pending;
    EnsureAvatarPendingLock();
    if (avatar_pending_lock_ == nullptr) return;
    if (xSemaphoreTake(avatar_pending_lock_, portMAX_DELAY) == pdTRUE) {
        pending = avatar_pending_;
        avatar_pending_ = PendingAvatarState{};
        xSemaphoreGive(avatar_pending_lock_);
    }
    if (pending.has_off) {
        SetAvatarOff();
        return;
    }
    if (pending.has_face) {
        SetAvatarExpression(pending.face_name.c_str());
    }
    if (pending.has_mouth) {
        SetMouthShape(pending.mouth_shape.c_str());
    }
    if (pending.has_blink) {
        if (pending.blink_enabled) {
            StartBlinkTimer();
        } else {
            StopBlinkTimer();
        }
    }

}

bool StackChanBoard::EnsureAvatarObject() {

    if (avatar_img_ != nullptr) {
        return true;
    }
    lv_obj_t* screen = lv_screen_active();
    if (screen == nullptr) {
        return false;
    }
    avatar_img_ = lv_image_create(screen);
    if (avatar_img_ == nullptr) {
        return false;
    }
    // Center on the 320x240 LCD and upscale 160x120 -> ~320x240 (2x).
    // lv_image_set_scale uses 256 = 1.0x; 512 = 2.0x.
    lv_image_set_scale(avatar_img_, 512);
    lv_obj_align(avatar_img_, LV_ALIGN_CENTER, 0, 0);
    lv_obj_clear_flag(avatar_img_, LV_OBJ_FLAG_SCROLLABLE);
    // Keep the avatar visually on top of the chat UI's emoji_label_,
    // chat bubbles, etc. The status bar (clock/battery) lives on a
    // separate sibling and is moved to foreground later if needed.
    lv_obj_move_foreground(avatar_img_);
    // If any overlay is already up, keep it above the freshly created
    // avatar (same sibling-ordering concern as RenderAvatarLocked).
    PromoteOverlaysLocked();
    ESP_LOGI(TAG, "Avatar lv_image created on active screen");
    return true;

}

bool StackChanBoard::SetAvatarExpressionLocked(const char* face) {

    const int idx = FaceNameToIndex(face);
    if (idx < 0) return false;
    current_face_index_ = idx;
    active_layer_ = ActiveLayer::FACE;
    if (!RenderAvatarLocked()) return false;
    current_avatar_face_ = face;
    return true;

}

bool StackChanBoard::SetAvatarExpression(const char* face) {

    if (display_ == nullptr) {
        ESP_LOGW(TAG, "SetAvatarExpression('%s') ignored: display_ not ready", face);
        return false;
    }
    // Avatar set fetch in progress — record the request and return
    // success. ApplyPendingAvatarAfterFetch() will replay the latest
    // captured face when the fetch completes.
    if (DeferAvatarFaceIfFetching(face)) {
        return true;
    }
    bool was_off = (current_avatar_face_ == "off");
    bool ok;
    {
        DisplayLockGuard lock(display_);
        if (avatar_img_ != nullptr) {
            // Restore visibility if a previous SetAvatarOff() hid the
            // layer. Cheap no-op when the flag is already clear.
            lv_obj_clear_flag(avatar_img_, LV_OBJ_FLAG_HIDDEN);
        }
        ok = SetAvatarExpressionLocked(face);
    }
    if (!ok) {
        ESP_LOGW(TAG, "SetAvatarExpression('%s') deferred (face unknown or screen not ready)", face);
        return ok;
    }
    // Coming back from "off": if blink was on before going off, restart
    // it so the user does not have to re-issue set_blink. The default
    // experience is "blink follows the avatar".
    if (was_off && blink_enabled_before_off_) {
        StartBlinkTimer();
        ESP_LOGI(TAG, "Blink restored after avatar resume from OFF");
    }
    blink_enabled_before_off_ = false;
    return ok;

}

bool StackChanBoard::SetAvatarOff() {

    if (display_ == nullptr) {
        ESP_LOGW(TAG, "SetAvatarOff() ignored: display_ not ready");
        return false;
    }
    if (DeferAvatarOffIfFetching()) {
        return true;
    }
    // Capture the previous blink state only on the first transition
    // into "off". A repeated set_avatar("off") while already off must
    // not overwrite the saved state with the post-off (always-false)
    // blink_enabled_.
    if (current_avatar_face_ != "off") {
        blink_enabled_before_off_ = blink_enabled_;
    }
    // StopBlinkTimer() takes the display lock internally for the
    // resting-face restore step, so call it before grabbing our lock.
    StopBlinkTimer();
    {
        DisplayLockGuard lock(display_);
        if (avatar_img_ != nullptr) {
            lv_obj_add_flag(avatar_img_, LV_OBJ_FLAG_HIDDEN);
        }
    }
    current_avatar_face_ = "off";
    ESP_LOGI(TAG, "Avatar OFF: hidden + blink disabled (was %s)",
             blink_enabled_before_off_ ? "ON" : "OFF");
    return true;

}

bool StackChanBoard::SetAvatarExpressionIfActive(const char* face) {

    if (current_avatar_face_ == "off") {
        ESP_LOGD(TAG, "SetAvatarExpressionIfActive('%s') skipped: avatar is OFF", face);
        return false;
    }
    return SetAvatarExpression(face);

}

void StackChanBoard::InitializeAvatar() {

    ESP_LOGI(TAG, "Schedule avatar init (deferred until SetupUI completes)");
    esp_timer_create_args_t timer_args = {
        .callback = [](void* arg) {
            StackChanBoard* board = static_cast<StackChanBoard*>(arg);
            if (board->SetAvatarExpression("idle")) {
                ESP_LOGI(TAG, "Initial avatar (idle) installed");
                if (board->avatar_init_timer_ != nullptr) {
                    esp_timer_stop(board->avatar_init_timer_);
                }
            }
        },
        .arg = this,
        .dispatch_method = ESP_TIMER_TASK,
        .name = "avatar_init",
        .skip_unhandled_events = true,
    };
    ESP_ERROR_CHECK(esp_timer_create(&timer_args, &avatar_init_timer_));
    // Retry every 500 ms; SetupUI() typically completes within a few
    // hundred ms after Application::Start(). Once installed the callback
    // stops the timer.
    ESP_ERROR_CHECK(esp_timer_start_periodic(avatar_init_timer_, 500 * 1000));

}

bool StackChanBoard::RestoreCurrentFaceLocked() {

    current_eyes_index_ = 0;
    active_layer_ = ActiveLayer::FACE;
    return RenderAvatarLocked();

}

bool StackChanBoard::SetMouthShape(const char* shape) {

    if (display_ == nullptr) {
        ESP_LOGW(TAG, "SetMouthShape('%s') ignored: display_ not ready", shape);
        return false;
    }
    const int idx = MouthShapeToIndex(shape);
    if (idx < 0) return false;
    if (DeferAvatarMouthIfFetching(shape)) {
        // Fetch in progress — record the request in avatar_pending_ and
        // let the post-fetch replay path apply it. Matches the face / off
        // / blink deferral paths so set_mouth doesn't race the AvatarSet
        // buffer swap.
        return true;
    }
    DisplayLockGuard lock(display_);
    current_mouth_index_ = idx;
    active_layer_ = ActiveLayer::MOUTH;
    return RenderAvatarLocked();

}
