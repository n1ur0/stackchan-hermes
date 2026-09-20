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

void StackChanBoard::InitializePowerSaveTimer() {

    // yorishiro: shutdown disabled (-1). The vessel must stay
    // powered through long gateway outages — with the upstream
    // 300 s value the AXP2101 powers the board off 5 minutes
    // after a WebSocket disconnect, even on USB power, and only
    // a physical long-press can bring it back. Reconnect already
    // retries forever, so staying on is enough to self-heal.
    // Display dimming (60 s) is kept.
    power_save_timer_ = new PowerSaveTimer(-1, 60, -1);
    power_save_timer_->OnEnterSleepMode([this]() {
        GetDisplay()->SetPowerSaveMode(true);
        GetBacklight()->SetBrightness(10);
    });
    power_save_timer_->OnExitSleepMode([this]() {
        GetDisplay()->SetPowerSaveMode(false);
        GetBacklight()->RestoreBrightness();
    });
    power_save_timer_->OnShutdownRequest([this]() {
        pmic_->PowerOff();
    });
    power_save_timer_->SetEnabled(true);

}

void StackChanBoard::InitializeI2c() {

    // Initialize I2C peripheral
    i2c_master_bus_config_t i2c_bus_cfg = {
        .i2c_port = (i2c_port_t)1,
        .sda_io_num = AUDIO_CODEC_I2C_SDA_PIN,
        .scl_io_num = AUDIO_CODEC_I2C_SCL_PIN,
        .clk_source = I2C_CLK_SRC_DEFAULT,
        .glitch_ignore_cnt = 7,
        .intr_priority = 0,
        .trans_queue_depth = 0,
        .flags = {
            .enable_internal_pullup = 1,
        },
    };
    ESP_ERROR_CHECK(i2c_new_master_bus(&i2c_bus_cfg, &i2c_bus_));

}

void StackChanBoard::InitializePortAI2c() {

    // Grove Port A bus. Uses I2C controller 0 (the internal bus above
    // uses controller 1) so the two run independently. Attached Unit
    // modules typically include their own 10 kΩ pull-ups in the Grove
    // hub, but enable internal pull-ups as a fall-back for bare wiring.
    i2c_master_bus_config_t port_a_cfg = {
        .i2c_port = (i2c_port_t)0,
        .sda_io_num = PORT_A_I2C_SDA_PIN,
        .scl_io_num = PORT_A_I2C_SCL_PIN,
        .clk_source = I2C_CLK_SRC_DEFAULT,
        .glitch_ignore_cnt = 7,
        .intr_priority = 0,
        .trans_queue_depth = 0,
        .flags = {
            .enable_internal_pullup = 1,
        },
    };
    ESP_ERROR_CHECK(i2c_new_master_bus(&port_a_cfg, &port_a_i2c_bus_));

}

void StackChanBoard::I2cDetect() {

    uint8_t address;
    printf("     0  1  2  3  4  5  6  7  8  9  a  b  c  d  e  f\r\n");
    for (int i = 0; i < 128; i += 16) {
        printf("%02x: ", i);
        for (int j = 0; j < 16; j++) {
            fflush(stdout);
            address = i + j;
            esp_err_t ret = i2c_master_probe(i2c_bus_, address, pdMS_TO_TICKS(200));
            if (ret == ESP_OK) {
                printf("%02x ", address);
            } else if (ret == ESP_ERR_TIMEOUT) {
                printf("UU ");
            } else {
                printf("-- ");
            }
        }
        printf("\r\n");
    }

}

void StackChanBoard::InitializeAxp2101() {

    ESP_LOGI(TAG, "Init AXP2101");
    pmic_ = new Pmic(i2c_bus_, 0x34);

}

void StackChanBoard::InitializeAw9523() {

    ESP_LOGI(TAG, "Init AW9523");
    aw9523_ = new Aw9523(i2c_bus_, 0x58);
    vTaskDelay(pdMS_TO_TICKS(50));

}

StackChanBoard::StackChanBoard() {

    InitializePowerSaveTimer();
    InitializeI2c();
    InitializePortAI2c();
    InitializeAxp2101();
    InitializeAw9523();
    // I2cDetect() moved AFTER all I2C device initializations.
    // The 128-address probe (i2c_master_probe over the whole bus) was
    // leaving PY32 (0x6F) in a half-finished slave state, so the
    // following transmit_receive (REG_VERSION via Repeated Start)
    // timed out (0x103). Doing the scan after IOExpander/Si12T init
    // preserves the boot-log debug info without poisoning subsequent
    // register reads. Si12T (0x68) is unaffected on the same bus,
    // but moving the scan is safer for any future I2C peripheral too.
    InitializeSpi();
    InitializeIli9342Display();
    InitializeCamera();
    InitializeFt6336TouchPad();
    GetBacklight()->RestoreBrightness();
    InitializeIOExpander();
    InitializeServo();
    InitializeSi12tTouch();
    InitializeLtr553Proximity();
    I2cDetect();
    // Avatar auto-display disabled: WiFi config UI needs to be visible.
    // Avatar is shown on-demand via MCP set_avatar command.
    // InitializeAvatar();
    InitializeMouthSequenceTask();
    RegisterMcpTools();

}

Display* StackChanBoard::GetDisplay() {

    return display_;

}

bool StackChanBoard::GetBatteryLevel(int &level, bool& charging, bool& discharging) {

    static bool last_discharging = false;
    charging = pmic_->IsCharging();
    discharging = pmic_->IsDischarging();
    if (discharging != last_discharging) {
        power_save_timer_->SetEnabled(discharging);
        last_discharging = discharging;
    }

    level = pmic_->GetBatteryLevel();
    return true;

}

void StackChanBoard::SetPowerSaveLevel(PowerSaveLevel level) {

    if (level != PowerSaveLevel::LOW_POWER) {
        power_save_timer_->WakeUp();
    }
    WifiBoard::SetPowerSaveLevel(level);

}

Backlight * StackChanBoard::GetBacklight() {

    static CustomBacklight backlight(pmic_);
    return &backlight;

}


DECLARE_BOARD(StackChanBoard);