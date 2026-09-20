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

esp_err_t StackChanBoard::InitPortBWs2812(uint16_t led_count) {

    if (ws2812_ok_ && ws2812_led_count_ == led_count) {
        return ESP_OK;  // idempotent: same led_count is a no-op
    }
    if (ws2812_handle_ != nullptr) {
        led_strip_del(ws2812_handle_);
        ws2812_handle_ = nullptr;
        ws2812_ok_ = false;
        ws2812_led_count_ = 0;
    }

    led_strip_config_t strip_config = {
        .strip_gpio_num = PORT_B_WS2812_DATA_PIN,
        .max_leds = led_count,
        .led_model = LED_MODEL_WS2812,
        .color_component_format = LED_STRIP_COLOR_COMPONENT_FMT_GRB,
        .flags = { .invert_out = false },
    };
    led_strip_rmt_config_t rmt_config = {
        .clk_src = RMT_CLK_SRC_DEFAULT,
        .resolution_hz = 10 * 1000 * 1000,  // 10 MHz, standard WS2812 bit timing
        .mem_block_symbols = 0,              // 0 = driver default block size
        .flags = { .with_dma = false },
    };
    esp_err_t err = led_strip_new_rmt_device(&strip_config, &rmt_config, &ws2812_handle_);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "port_b.ws2812.init led_strip_new_rmt_device failed: %s",
                 esp_err_to_name(err));
        return err;
    }

    err = led_strip_clear(ws2812_handle_);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "port_b.ws2812.init led_strip_clear failed: %s",
                 esp_err_to_name(err));
        led_strip_del(ws2812_handle_);
        ws2812_handle_ = nullptr;
        return err;
    }
    ws2812_led_count_ = led_count;
    ws2812_ok_ = true;
    ESP_LOGI(TAG, "port_b.ws2812 initialized: %u LEDs on GPIO %d",
             (unsigned)led_count, (int)PORT_B_WS2812_DATA_PIN);
    return ESP_OK;

}

void StackChanBoard::InitializeIOExpander() {

    ESP_LOGI(TAG, "Init PY32 IO expander (I2C addr 0x%02X)", Py32IoExpander::DEFAULT_ADDR);
    io_expander_ = std::unique_ptr<Py32IoExpander>(new Py32IoExpander(i2c_bus_));

    // PY32 boots slowly and is unreliable in the first few hundred ms
    // after power-on. Retry the probe up to 5 times with 500ms gaps —
    // total budget ~2.5 s, which dominates boot latency by maybe 1.5 s
    // in the worst case but is still well under the time spent on
    // I2C scan + LCD panel init that happen earlier.
    constexpr int kBeginRetries  = 5;
    constexpr int kBeginDelayMs  = 500;
    bool   ok = false;
    uint8_t version = 0;
    int     winning_attempt = 0;
    for (int i = 0; i < kBeginRetries; i++) {
        vTaskDelay(pdMS_TO_TICKS(kBeginDelayMs));
        if (io_expander_->Begin(&version)) {
            ok = true;
            winning_attempt = i + 1;
            break;
        }
        ESP_LOGW(TAG, "PY32 not responding, retry %d/%d", i + 1, kBeginRetries);
    }

    if (!ok) {
        ESP_LOGE(TAG, "PY32 IO expander FAILED after %d attempts; servo will be POWERLESS",
                 kBeginRetries);
        io_expander_.reset();
        return;
    }
    ESP_LOGI(TAG, "PY32 IO expander READY (version=0x%02X, attempt=%d/%d)",
             version, winning_attempt, kBeginRetries);

    // Pin 0 = VM EN (servo power switch). Output, pull-up, drive HIGH.
    // We track each step so a partial success is reported precisely
    // (e.g. direction set but pull-up failed) — much easier to debug
    // than the previous "all-void, hope it stuck" version.
    bool ok_dir   = io_expander_->SetDirection(0, true);
    bool ok_pull  = io_expander_->SetPullMode(0, true);
    bool ok_write = io_expander_->DigitalWrite(0, true);
    vTaskDelay(pdMS_TO_TICKS(200));

    if (!ok_dir || !ok_pull || !ok_write) {
        const char* failed = "?";
        if (!ok_dir)        failed = "SetDirection";
        else if (!ok_pull)  failed = "SetPullMode";
        else if (!ok_write) failed = "DigitalWrite";
        ESP_LOGE(TAG, "Servo power ENABLE FAILED at step=%s", failed);
        return;
    }

    // Verify by reading back the output low-byte register. Bit 0 must
    // be high. If not, the chip ACK'd but the level didn't latch — log
    // it loudly so we know the next move_head will be silent.
    uint8_t out_low = 0;
    if (io_expander_->ReadOutputLow(&out_low)) {
        if (out_low & 0x01) {
            ESP_LOGI(TAG, "Servo power ENABLED via PY32 pin 0 "
                          "(VM EN HIGH confirmed, REG_GPIO_O_L=0x%02X)", out_low);
        } else {
            ESP_LOGE(TAG, "Servo power write succeeded but readback shows "
                          "pin 0 LOW (REG_GPIO_O_L=0x%02X) — VM EN may be off!",
                          out_low);
        }
    } else {
        // Read failed but writes succeeded; assume the writes took.
        ESP_LOGW(TAG, "Servo power writes OK, but readback verify failed "
                      "(can't confirm VM EN level)");
    }

    // ---- RGB strip init (12x WS2812C on the StackChan base) ----
    // The data line is on PY32 pin 13 (not an ESP32 GPIO); the PY32
    // bit-bangs the WS2812 protocol itself. We just write RGB565 into
    // its LED RAM and toggle the latch bit. Sequence is the same as the
    // M5 BSP: configure pin 13 as push-pull output with pull-up,
    // SetLedCount(12), small settle delay, then clear all LEDs.
    bool ok_d   = io_expander_->SetDirection(RGB_DATA_PIN, true);
    bool ok_p   = io_expander_->SetPullMode(RGB_DATA_PIN, true);
    bool ok_dr  = io_expander_->SetDriveMode(RGB_DATA_PIN, false);
    bool ok_cnt = io_expander_->SetLedCount(RGB_LED_COUNT);
    if (!ok_d || !ok_p || !ok_dr || !ok_cnt) {
        const char* failed = "?";
        if      (!ok_d)   failed = "SetDirection(13)";
        else if (!ok_p)   failed = "SetPullMode(13)";
        else if (!ok_dr) failed = "SetDriveMode(13)";
        else if (!ok_cnt) failed = "SetLedCount";
        ESP_LOGE(TAG, "RGB strip init FAILED at step=%s; LEDs disabled", failed);
        return;
    }
    // M5 reference firmware waits 200 ms after SetLedCount before the
    // first refresh — the PY32 internal LED engine needs the settle.
    vTaskDelay(pdMS_TO_TICKS(200));

    // Clear strip: zero RAM in one burst, then latch.
    uint8_t clear_buf[RGB_LED_COUNT * 2] = {0};
    bool ok_clear = io_expander_->SetLedData(clear_buf, sizeof(clear_buf));
    bool ok_ref   = io_expander_->RefreshLeds();
    if (!ok_clear || !ok_ref) {
        ESP_LOGE(TAG, "RGB strip clear FAILED (data=%d refresh=%d); LEDs disabled",
                 ok_clear, ok_ref);
        return;
    }
    rgb_ok_ = true;
    ESP_LOGI(TAG, "RGB strip READY (%d WS2812C via PY32 pin %d, all cleared)",
             RGB_LED_COUNT, RGB_DATA_PIN);

}

uint8_t StackChanBoard::ClampByte(int v) {

    if (v < 0) return 0;
    if (v > 255) return 255;
    return (uint8_t)v;

}

bool StackChanBoard::JsonByte(cJSON* item, uint8_t* out) {

    if (!cJSON_IsNumber(item)) return false;
    if (item->valuedouble != static_cast<double>(item->valueint)) return false;
    if (item->valueint < 0 || item->valueint > 255) return false;
    *out = static_cast<uint8_t>(item->valueint);
    return true;

}

void StackChanBoard::PackRgb565(uint8_t r, uint8_t g, uint8_t b, uint8_t out[2]) {

    uint16_t v = (uint16_t)(((r & 0xF8) << 8) | ((g & 0xFC) << 3) | (b >> 3));
    out[0] = (uint8_t)(v & 0xFF);
    out[1] = (uint8_t)((v >> 8) & 0xFF);

}

void StackChanBoard::SetAllRgbLeds(uint8_t r, uint8_t g, uint8_t b) {

    if (!rgb_ok_ || io_expander_ == nullptr) {
        return;
    }
    uint8_t buf[RGB_LED_COUNT * 2];
    uint8_t pair[2];
    PackRgb565(r, g, b, pair);
    for (int i = 0; i < RGB_LED_COUNT; i++) {
        buf[i * 2 + 0] = pair[0];
        buf[i * 2 + 1] = pair[1];
    }
    if (io_expander_->SetLedData(buf, sizeof(buf))) {
        io_expander_->RefreshLeds();
    }

}

