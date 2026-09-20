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

void StackChanBoard::InitializeCamera() {

    static esp_cam_ctlr_dvp_pin_config_t dvp_pin_config = {
        .data_width = CAM_CTLR_DATA_WIDTH_8,
        .data_io = {
            [0] = CAMERA_PIN_D0,
            [1] = CAMERA_PIN_D1,
            [2] = CAMERA_PIN_D2,
            [3] = CAMERA_PIN_D3,
            [4] = CAMERA_PIN_D4,
            [5] = CAMERA_PIN_D5,
            [6] = CAMERA_PIN_D6,
            [7] = CAMERA_PIN_D7,
        },
        .vsync_io = CAMERA_PIN_VSYNC,
        .de_io = CAMERA_PIN_HREF,
        .pclk_io = CAMERA_PIN_PCLK,
        .xclk_io = CAMERA_PIN_XCLK,
    };

    esp_video_init_sccb_config_t sccb_config = {
        .init_sccb = false,
        .i2c_handle = i2c_bus_,
        .freq = 100000,
    };

    esp_video_init_dvp_config_t dvp_config = {
        .sccb_config = sccb_config,
        .reset_pin = CAMERA_PIN_RESET,
        .pwdn_pin = CAMERA_PIN_PWDN,
        .dvp_pin = dvp_pin_config,
        .xclk_freq = XCLK_FREQ_HZ,
    };

    esp_video_init_config_t video_config = {
        .dvp = &dvp_config,
    };

    camera_ = new EspVideo(video_config);
    camera_->SetHMirror(false);

}

Camera* StackChanBoard::GetCamera() {

    return camera_;

}

