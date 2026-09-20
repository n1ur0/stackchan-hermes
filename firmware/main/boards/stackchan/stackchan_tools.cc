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

void StackChanBoard::RegisterMcpTools() {

    auto& mcp_server = McpServer::GetInstance();
    ESP_LOGI(TAG, "Registering StackChan MCP tools...");

    // Set head angles (yaw, pitch in degrees)
    // SCS0009: 1 step = 0.3125 degrees, so 1 degree = 3.2 steps (= 16/5)
    // yaw: -90..90 degrees (no hardware restriction). pitch: two-tier
    // guard — see SAFE_PITCH_MIN/MAX (hard clamp for mechanical safety)
    // and RECOMMENDED_PITCH_MIN/MAX (M5Stack-documented operating sweet
    // spot) above, plus Issue #80 / #98.
    mcp_server.AddTool(
        "self.robot.set_head_angles",
        "Set the head angles of the robot. yaw: horizontal (-90 to 90). pitch: vertical. M5Stack-recommended operating range is 5 to 85 degrees per https://docs.m5stack.com/en/StackChan (\"Motion Angle Notice\"). The firmware also accepts values up to 88 degrees (the hard clamp guards against the audible sub-stall observed at pitch=89 on real hardware), but values outside 5-85 degrees are not officially endorsed and may stress the servo over time. Requests below 0 degrees or above 88 degrees are silently clamped with an ESP_LOGW. Optional speed_dps: angular speed in degrees per second. If omitted or zero, the existing duration-based default applies. See README \"Hardware safety notes\".",
        // Pitch schema range is intentionally permissive across the
        // entire `int` value range (std::numeric_limits<int>::min/max):
        // the authoritative Tier 1 enforcement lives in the handler
        // below (silent clamp to [SAFE_PITCH_MIN, SAFE_PITCH_MAX] with
        // ESP_LOGW). Any narrower range would cause McpServer::Property
        // to reject sufficiently-extreme requests (e.g. pitch=200 or
        // pitch=INT_MIN) before the handler can run, leaving the
        // Tier 1 clamp / log unreachable for those callers and
        // contradicting the tool-description / README claim that
        // out-of-range requests are silently clamped with ESP_LOGW —
        // see Issue #98 (three adversarial-review rounds zeroed in on
        // this exact contract, including the int-boundary corners) and
        // PR #81's defense-in-depth requirement that every servo-write
        // boundary be guarded inside the firmware regardless of
        // caller behavior.
        PropertyList({Property("yaw", kPropertyTypeInteger, 0, -90, 90),
                      Property("pitch", kPropertyTypeInteger, 0,
                               std::numeric_limits<int>::min(),
                               std::numeric_limits<int>::max()),
                      Property("speed_dps", kPropertyTypeInteger, 0,
                               std::numeric_limits<int>::min(),
                               std::numeric_limits<int>::max())}),
        [this](const PropertyList& properties) -> ReturnValue {
            int yaw = properties["yaw"].value<int>();
            int pitch = properties["pitch"].value<int>();
            int speed_dps = properties["speed_dps"].value<int>();
            // Issue #80 / #98: two-tier pitch guard.
            //
            // Tier 1 (hard clamp): silently clamp to [SAFE_PITCH_MIN,
            // SAFE_PITCH_MAX] and ESP_LOGW. PitchDegToPos() clamps
            // again at the servo-write boundary (defense-in-depth);
            // doing it here lets us log the original out-of-range
            // value. See the SAFE_PITCH_MIN/MAX comment block above.
            if (pitch < SAFE_PITCH_MIN) {
                ESP_LOGW(TAG, "set_head_angles: pitch=%d below SAFE_PITCH_MIN=%d, clamping (servo end-stop protection)",
                         pitch, SAFE_PITCH_MIN);
                pitch = SAFE_PITCH_MIN;
            }
            if (pitch > SAFE_PITCH_MAX) {
                ESP_LOGW(TAG, "set_head_angles: pitch=%d above SAFE_PITCH_MAX=%d, clamping (servo end-stop protection)",
                         pitch, SAFE_PITCH_MAX);
                pitch = SAFE_PITCH_MAX;
            }
            // Tier 2 (recommended-range soft signal): inside the hard
            // clamp but outside the M5Stack-documented operating
            // range — accept the value and emit an ESP_LOGI so callers
            // can notice the deviation without blocking the motion.
            if (pitch < RECOMMENDED_PITCH_MIN || pitch > RECOMMENDED_PITCH_MAX) {
                ESP_LOGI(TAG, "set_head_angles: pitch=%d outside M5Stack-recommended range %d..%d (within hard clamp %d..%d); acceptable but not officially endorsed",
                         pitch, RECOMMENDED_PITCH_MIN, RECOMMENDED_PITCH_MAX,
                         SAFE_PITCH_MIN, SAFE_PITCH_MAX);
            }
            int yaw_pos = YawDegToPos(yaw);
            int pitch_pos = PitchDegToPos(pitch);
            if (speed_dps > 0) {
                WriteHeadAngles(yaw, pitch, speed_dps);
            } else {
                WriteHeadAngles(yaw, pitch);
            }
            // Re-arm the idle backstop: an explicit move counts as
            // activity, so the 60 s auto-settle is measured from here.
            ScheduleIdleSettle();
            bool yaw_motion_started = false;
            bool pitch_motion_started = false;
            if (servo_ok_) {
                xSemaphoreTake(motion_mutex_, portMAX_DELAY);
                yaw_motion_started = yaw_motion_.moving;
                pitch_motion_started = pitch_motion_.moving;
                xSemaphoreGive(motion_mutex_);
            }
            ESP_LOGI(TAG, "set_head_angles: yaw=%d (pos=%d) motion_started=%d, pitch=%d (pos=%d) motion_started=%d, uart=%d, servo_ok=%d",
                     yaw, yaw_pos, yaw_motion_started, pitch, pitch_pos, pitch_motion_started, (int)SERVO_UART_NUM, servo_ok_);
            cJSON* root = cJSON_CreateObject();
            cJSON_AddBoolToObject(root, "servo_init_ok", servo_ok_);
            cJSON_AddNumberToObject(root, "uart_num", (int)SERVO_UART_NUM);
            cJSON_AddNumberToObject(root, "yaw_pos", yaw_pos);
            cJSON_AddNumberToObject(root, "pitch_pos", pitch_pos);
            cJSON_AddNumberToObject(root, "yaw_motion_started", yaw_motion_started ? 1 : 0);
            cJSON_AddNumberToObject(root, "pitch_motion_started", pitch_motion_started ? 1 : 0);
            return root;
        });

    // Persist the neutral (rest) pose so the head's resting yaw/pitch can
    // be retuned from the dashboard without a reflash. The values drive
    // boot-init, the proximity/touch revert (TouchRevertCb) and the
    // idle-settle return. Saving also moves the head there immediately so
    // the operator can confirm the pose. Both values persist across
    // reboots (NVS namespace "stackchan_pose").
    // Pitch schema range is intentionally permissive (whole int range);
    // the authoritative clamp to [SAFE_PITCH_MIN, SAFE_PITCH_MAX] lives in
    // the handler with an ESP_LOGW, mirroring set_head_angles (see the
    // PropertyList rationale there / Issue #98).
    mcp_server.AddTool(
        "self.robot.set_neutral_pose",
        "Set and persist the neutral (rest) head pose. yaw: horizontal "
        "(-90 to 90). pitch: vertical; higher looks up. The firmware "
        "accepts pitch 0 to 88 (silently clamped with an ESP_LOGW outside "
        "that range). This pose is used at boot and whenever the head "
        "recenters after a proximity/touch reaction or idle timeout. "
        "Saving moves the head to the new pose immediately as a "
        "confirmation. Both values persist across reboots.",
        PropertyList({Property("yaw", kPropertyTypeInteger, 0, -90, 90),
                      Property("pitch", kPropertyTypeInteger, 0,
                               std::numeric_limits<int>::min(),
                               std::numeric_limits<int>::max())}),
        [this](const PropertyList& properties) -> ReturnValue {
            int yaw = properties["yaw"].value<int>();
            int pitch = properties["pitch"].value<int>();
            // Tier 1 (hard clamp): silently clamp to [SAFE_PITCH_MIN,
            // SAFE_PITCH_MAX] and ESP_LOGW, same as set_head_angles. yaw
            // is already schema-bounded to [-90, 90], but clamp
            // defensively in case the schema bound is ever widened.
            if (yaw < -90) yaw = -90;
            if (yaw > 90)  yaw = 90;
            if (pitch < SAFE_PITCH_MIN) {
                ESP_LOGW(TAG, "set_neutral_pose: pitch=%d below SAFE_PITCH_MIN=%d, clamping (servo end-stop protection)",
                         pitch, SAFE_PITCH_MIN);
                pitch = SAFE_PITCH_MIN;
            }
            if (pitch > SAFE_PITCH_MAX) {
                ESP_LOGW(TAG, "set_neutral_pose: pitch=%d above SAFE_PITCH_MAX=%d, clamping (servo end-stop protection)",
                         pitch, SAFE_PITCH_MAX);
                pitch = SAFE_PITCH_MAX;
            }
            {
                Settings settings("stackchan_pose", true);
                settings.SetInt("yaw", yaw);
                settings.SetInt("pitch", pitch);
            }
            neutral_yaw_   = yaw;
            neutral_pitch_ = pitch;
            ESP_LOGI(TAG, "neutral pose updated: yaw=%d pitch=%d", yaw, pitch);
            // Move to the new neutral immediately so the operator can
            // confirm the pose on the real device.
            WriteHeadAngles(neutral_yaw_, neutral_pitch_);
            ScheduleIdleSettle();
            cJSON* root = cJSON_CreateObject();
            cJSON_AddNumberToObject(root, "yaw", yaw);
            cJSON_AddNumberToObject(root, "pitch", pitch);
            return root;
        });

    // Get current head angles
    mcp_server.AddTool(
        "self.robot.get_head_angles",
        "Get the current head angles (yaw, pitch) of the robot in degrees. "
        "Returns {\"yaw\":N,\"pitch\":N} on success; "
        "on persistent ReadPos failure returns "
        "{\"yaw\":null,\"pitch\":null,\"error\":...,\"servo_ok\":bool,"
        "\"yaw_attempts\":N,\"pitch_attempts\":N}.",
        PropertyList(),
        [this](const PropertyList& properties) -> ReturnValue {
            // Issue #123: retry ReadPos a few times before falling back
            // to an explicit error reply. Single-call ReadPos failures
            // (e.g. servo mid-motion, transient bus contention) are a
            // known transient mode that InitializeServo() already treats
            // as warning-and-continue; the previous behaviour of running
            // `ReadPos==-1` through the same `(pos-zero)*5/16` math as a
            // valid position produced sentinel `{-144,-194}` that was
            // indistinguishable from a genuine bus hang at the MCP layer
            // (see #1 / #100 / #118 hang judgments).
            constexpr int kReadPosRetryMax = 3;
            constexpr uint32_t kReadPosRetryDelayMs = 50;

            int yaw_pos = -1;
            int pitch_pos = -1;
            int yaw_attempts = 0;
            int pitch_attempts = 0;
            if (servo_ok_) {
                xSemaphoreTake(scs_bus_mutex_, portMAX_DELAY);
                for (int i = 0; i < kReadPosRetryMax; i++) {
                    yaw_attempts = i + 1;
                    yaw_pos = scs_bus_.ReadPos(SERVO_YAW_ID);
                    if (yaw_pos >= 0) break;
                    if (i + 1 < kReadPosRetryMax) {
                        vTaskDelay(pdMS_TO_TICKS(kReadPosRetryDelayMs));
                    }
                }
                for (int i = 0; i < kReadPosRetryMax; i++) {
                    pitch_attempts = i + 1;
                    pitch_pos = scs_bus_.ReadPos(SERVO_PITCH_ID);
                    if (pitch_pos >= 0) break;
                    if (i + 1 < kReadPosRetryMax) {
                        vTaskDelay(pdMS_TO_TICKS(kReadPosRetryDelayMs));
                    }
                }
                xSemaphoreGive(scs_bus_mutex_);
            }

            cJSON* root = cJSON_CreateObject();
            const bool yaw_ok = yaw_pos >= 0;
            const bool pitch_ok = pitch_pos >= 0;
            if (yaw_ok && pitch_ok) {
                int yaw = (yaw_pos - 460) * 5 / 16;
                int pitch = (pitch_pos - 620) * 5 / 16;
                cJSON_AddNumberToObject(root, "yaw", yaw);
                cJSON_AddNumberToObject(root, "pitch", pitch);
            } else {
                cJSON_AddNullToObject(root, "yaw");
                cJSON_AddNullToObject(root, "pitch");
                char err[160];
                snprintf(err, sizeof(err),
                         "ReadPos failed: yaw_raw=%d (attempts=%d) "
                         "pitch_raw=%d (attempts=%d) servo_ok=%d",
                         yaw_pos, yaw_attempts,
                         pitch_pos, pitch_attempts,
                         servo_ok_ ? 1 : 0);
                cJSON_AddStringToObject(root, "error", err);
                cJSON_AddBoolToObject(root, "servo_ok", servo_ok_);
                cJSON_AddNumberToObject(root, "yaw_attempts", yaw_attempts);
                cJSON_AddNumberToObject(root, "pitch_attempts", pitch_attempts);
            }
            // Always surface the persisted neutral (rest) pose so the
            // dashboard can show / pre-fill the set_neutral_pose form even
            // when the live ReadPos failed.
            cJSON_AddNumberToObject(root, "neutral_yaw", neutral_yaw_);
            cJSON_AddNumberToObject(root, "neutral_pitch", neutral_pitch_);
            char* str = cJSON_PrintUnformatted(root);
            std::string result(str);
            cJSON_free(str);
            cJSON_Delete(root);
            ESP_LOGI(TAG,
                     "get_head_angles: servo_ok=%d yaw_raw=%d (attempts=%d) "
                     "pitch_raw=%d (attempts=%d) result=%s",
                     servo_ok_ ? 1 : 0,
                     yaw_pos, yaw_attempts,
                     pitch_pos, pitch_attempts,
                     result.c_str());
            return result;
        });

    mcp_server.AddTool(
        "self.robot.set_servo_torque",
        "Enable or disable SCS0009 servo torque on the yaw / pitch axes "
        "independently. Disabling torque stops motor current on that axis; "
        "the head holds via static friction (no motion is commanded). "
        "On disable, the corresponding axis's MotionDriver state is reset "
        "(moving=false, position_unknown=true, request token invalidated) "
        "so a stale interpolation cannot resume on the bus and a "
        "subsequent same-target set_head_angles is re-dispatched rather "
        "than no-op-optimized. Re-enabling torque does NOT trigger a "
        "move -- the next set_head_angles or wobble call will. Returns "
        "the per-axis bus return codes. Diagnostic / power-management "
        "primitive; auto release on idle is tracked separately under "
        "#152 Phase 4.",
        PropertyList({Property("yaw_enabled", kPropertyTypeBoolean),
                      Property("pitch_enabled", kPropertyTypeBoolean)}),
        [this](const PropertyList& properties) -> ReturnValue {
            bool yaw_enabled = properties["yaw_enabled"].value<bool>();
            bool pitch_enabled = properties["pitch_enabled"].value<bool>();
            ServoTorqueResult torque_result = InternalSetServoTorque(
                yaw_enabled, pitch_enabled, ReleaseReason::kManual);

            cJSON* root = cJSON_CreateObject();
            cJSON_AddBoolToObject(root, "yaw_enabled", yaw_enabled);
            cJSON_AddBoolToObject(root, "pitch_enabled", pitch_enabled);
            cJSON_AddNumberToObject(root, "yaw_bus_return",
                                    torque_result.yaw_bus_return);
            cJSON_AddNumberToObject(root, "pitch_bus_return",
                                    torque_result.pitch_bus_return);
            cJSON_AddBoolToObject(root, "servo_ok", servo_ok_);
            // Issue #171: ok counts an idempotent no-op as success but a
            // wait-budget exhaustion as failure (the requested torque
            // transition did not actually happen on the bus).
            cJSON_AddBoolToObject(
                root, "ok",
                servo_ok_ && (torque_result.idempotent_short_circuit ||
                              (torque_result.yaw_ok &&
                               torque_result.pitch_ok)));
            // Issue #171: the old single `short_circuited` field is
            // removed (no alias). These two orthogonal, mutually
            // exclusive flags let callers distinguish a degraded-bus
            // wait-exhaustion from an idempotent no-op success.
            cJSON_AddBoolToObject(root, "idempotent_short_circuit",
                                  torque_result.idempotent_short_circuit);
            cJSON_AddBoolToObject(root, "wait_exhausted",
                                  torque_result.wait_exhausted);
            if (!servo_ok_) {
                cJSON_AddStringToObject(root, "error",
                                        "Servo bus not initialized.");
            }
            return root;
        });

    mcp_server.AddTool(
        "self.robot.set_auto_torque_release",
        "Enable or disable automatic SCS0009 torque release after "
        "motion idle timeout. timeout_ms is clamped by the firmware "
        "to 500..600000 ms. Disabling this setting does not re-enable "
        "torque if it is already released; the next set_head_angles, "
        "wobble, or explicit set_servo_torque(true, true) call "
        "re-engages torque.",
        PropertyList({Property("enabled", kPropertyTypeBoolean),
                      Property("timeout_ms", kPropertyTypeInteger,
                               (int)AUTO_TORQUE_RELEASE_DEFAULT_MS)}),
        [this](const PropertyList& properties) -> ReturnValue {
            bool enabled = properties["enabled"].value<bool>();
            int requested_timeout_ms = properties["timeout_ms"].value<int>();
            bool clamped = false;
            uint32_t timeout_ms = 0;

            if (requested_timeout_ms <
                static_cast<int>(AUTO_TORQUE_RELEASE_MIN_MS)) {
                timeout_ms = AUTO_TORQUE_RELEASE_MIN_MS;
                clamped = true;
                ESP_LOGW(TAG,
                         "set_auto_torque_release: timeout_ms=%d below "
                         "minimum %u, clamping",
                         requested_timeout_ms,
                         (unsigned)AUTO_TORQUE_RELEASE_MIN_MS);
            } else if (requested_timeout_ms >
                       static_cast<int>(AUTO_TORQUE_RELEASE_MAX_MS)) {
                timeout_ms = AUTO_TORQUE_RELEASE_MAX_MS;
                clamped = true;
                ESP_LOGW(TAG,
                         "set_auto_torque_release: timeout_ms=%d above "
                         "maximum %u, clamping",
                         requested_timeout_ms,
                         (unsigned)AUTO_TORQUE_RELEASE_MAX_MS);
            } else {
                timeout_ms = static_cast<uint32_t>(requested_timeout_ms);
            }

            bool torque_released_at_call =
                torque_state_.load(std::memory_order_acquire) ==
                TorqueState::kReleased;
            auto_release_timeout_ms_.store(timeout_ms,
                                           std::memory_order_release);
            auto_release_enabled_.store(enabled,
                                        std::memory_order_release);

            ESP_LOGI(TAG,
                     "set_auto_torque_release: enabled=%d "
                     "timeout_ms=%u clamped=%d "
                     "torque_released_at_call=%d",
                     enabled ? 1 : 0, (unsigned)timeout_ms,
                     clamped ? 1 : 0,
                     torque_released_at_call ? 1 : 0);

            cJSON* root = cJSON_CreateObject();
            cJSON_AddBoolToObject(root, "enabled", enabled);
            cJSON_AddNumberToObject(root, "timeout_ms", timeout_ms);
            cJSON_AddBoolToObject(root, "clamped", clamped);
            cJSON_AddBoolToObject(root, "torque_released_at_call",
                                  torque_released_at_call);
            return root;
        });

    // Diagnostic: toggle GPIO6 (servo TX) HIGH/LOW to verify physical signal
    mcp_server.AddTool(
        "self.robot.gpio_test",
        "Diagnostic: toggle GPIO6 (servo TX pin) HIGH/LOW 5 times at 100ms intervals to verify physical signal output. Restores UART pins after.",
        PropertyList(),
        [](const PropertyList& properties) -> ReturnValue {
            cJSON* root = cJSON_CreateObject();

            gpio_num_t pin = static_cast<gpio_num_t>(SERVO_TX_PIN);

            esp_err_t err_dir = gpio_set_direction(pin, GPIO_MODE_OUTPUT);
            cJSON_AddStringToObject(root, "set_direction", esp_err_to_name(err_dir));
            cJSON_AddNumberToObject(root, "pin", SERVO_TX_PIN);

            cJSON* toggles = cJSON_CreateArray();
            for (int i = 0; i < 5; i++) {
                esp_err_t err_h = gpio_set_level(pin, 1);
                vTaskDelay(pdMS_TO_TICKS(100));
                esp_err_t err_l = gpio_set_level(pin, 0);
                vTaskDelay(pdMS_TO_TICKS(100));
                cJSON* item = cJSON_CreateObject();
                cJSON_AddNumberToObject(item, "iter", i);
                cJSON_AddStringToObject(item, "high", esp_err_to_name(err_h));
                cJSON_AddStringToObject(item, "low", esp_err_to_name(err_l));
                cJSON_AddItemToArray(toggles, item);
            }
            cJSON_AddItemToObject(root, "toggles", toggles);

            // Restore UART pin assignment after raw GPIO toggling
            esp_err_t err_restore = uart_set_pin(SERVO_UART_NUM, SERVO_TX_PIN, SERVO_RX_PIN,
                                                UART_PIN_NO_CHANGE, UART_PIN_NO_CHANGE);
            cJSON_AddStringToObject(root, "uart_pin_restore", esp_err_to_name(err_restore));

            char* str = cJSON_PrintUnformatted(root);
            std::string result(str);
            cJSON_free(str);
            cJSON_Delete(root);
            ESP_LOGI(TAG, "gpio_test: %s", result.c_str());
            return result;
        });

    // Diagnostic: send raw bytes via uart_write_bytes, equivalent to WritePos(1, 1000, 0, 0)
    mcp_server.AddTool(
        "self.robot.uart_diag",
        "Diagnostic: send raw 8 bytes (FF FF 01 04 03 E8 00 00) directly via uart_write_bytes. Returns sent byte count and rx buffer length before/after.",
        PropertyList(),
        [this](const PropertyList& properties) -> ReturnValue {
            cJSON* root = cJSON_CreateObject();

            size_t buf_before = 0;
            esp_err_t err_b = ESP_ERR_INVALID_STATE;
            int written = -1;
            esp_err_t err_wait = ESP_ERR_INVALID_STATE;
            esp_err_t err_a = ESP_ERR_INVALID_STATE;
            size_t buf_after = 0;
            const uint8_t bytes[] = {0xFF, 0xFF, 0x01, 0x04, 0x03, 0xE8, 0x00, 0x00};

            if (servo_ok_) {
                xSemaphoreTake(scs_bus_mutex_, portMAX_DELAY);

                err_b = uart_get_buffered_data_len(SERVO_UART_NUM, &buf_before);

                written = uart_write_bytes(SERVO_UART_NUM, (const char*)bytes, sizeof(bytes));

                // Wait for TX FIFO drain
                err_wait = uart_wait_tx_done(SERVO_UART_NUM, pdMS_TO_TICKS(100));

                vTaskDelay(pdMS_TO_TICKS(20));

                err_a = uart_get_buffered_data_len(SERVO_UART_NUM, &buf_after);

                xSemaphoreGive(scs_bus_mutex_);
            }
            cJSON_AddStringToObject(root, "buf_before_status", esp_err_to_name(err_b));
            cJSON_AddNumberToObject(root, "buf_before", buf_before);

            cJSON_AddNumberToObject(root, "written", written);
            cJSON_AddNumberToObject(root, "expected", (int)sizeof(bytes));

            cJSON_AddStringToObject(root, "tx_done_status", esp_err_to_name(err_wait));

            cJSON_AddStringToObject(root, "buf_after_status", esp_err_to_name(err_a));
            cJSON_AddNumberToObject(root, "buf_after", buf_after);

            char* str = cJSON_PrintUnformatted(root);
            std::string result(str);
            cJSON_free(str);
            cJSON_Delete(root);
            ESP_LOGI(TAG, "uart_diag: %s", result.c_str());
            return result;
        });

    // Diagnostic: read PY32 REG_GPIO_O_L (output low byte) and report
    // whether VM EN (pin 0) is HIGH. Used to investigate "servo stops
    // moving after the first move_head" — if VM EN drops to LOW under
    // load, the servo loses power even though the I2C write succeeds.
    mcp_server.AddTool(
        "self.robot.check_vm_en",
        "Diagnostic: read PY32 REG_GPIO_O_L and report whether VM EN (pin 0 = servo power) is currently HIGH. "
        "Returns {io_expander_present, i2c_read_ok, raw, vm_en_high}.",
        PropertyList(),
        [this](const PropertyList&) -> ReturnValue {
            cJSON* root = cJSON_CreateObject();
            bool present = (io_expander_ != nullptr);
            cJSON_AddBoolToObject(root, "io_expander_present", present);
            if (present) {
                uint8_t out_low = 0;
                bool ok = io_expander_->ReadOutputLow(&out_low);
                cJSON_AddBoolToObject(root, "i2c_read_ok", ok);
                if (ok) {
                    cJSON_AddNumberToObject(root, "raw", out_low);
                    cJSON_AddBoolToObject(root, "vm_en_high", (out_low & 0x01) != 0);
                }
            }
            ESP_LOGI(TAG, "check_vm_en queried");
            return root;
        });

    // Set the avatar face (one of: idle, happy, thinking, sad, surprised,
    // embarrassed, off).
    // The image is rendered as a 320x240 overlay on top of the chat UI's
    // emoji_label_ / emoji_image_; LVGL theme/Application emotion updates
    // will keep happening underneath but are visually masked.
    // 'off' hides the avatar lv_obj and disables blink so the underlying
    // xiaozhi-esp32 screens (WiFi config UI, OTA, settings) become visible.
    // A subsequent set_avatar with any other face brings it back, and
    // restores blink to whatever state it was in before going off.
    mcp_server.AddTool(
        "self.display.set_avatar",
        "Set the avatar face displayed on the LCD. face must be one of: "
        "idle, happy, thinking, sad, surprised, embarrassed, off. "
        "'off' hides the avatar and disables blink so the underlying "
        "xiaozhi-esp32 screens (WiFi config UI, OTA, settings) are "
        "visible; calling set_avatar with another face brings the avatar "
        "back and restores the previous blink state.",
        PropertyList({Property("face", kPropertyTypeString)}),
        [this](const PropertyList& properties) -> ReturnValue {
            std::string face = properties["face"].value<std::string>();
            cJSON* root = cJSON_CreateObject();
            cJSON_AddStringToObject(root, "face", face.c_str());

            bool applied = false;
            if (face == "off") {
                // Any avatar transition supersedes an in-flight mouth
                // sequence (per Issue #5 acceptance: "set_avatar() takes
                // effect after the queued sequence finishes (or
                // interrupts cleanly)").
                RequestMouthSequenceCancel();
                applied = SetAvatarOff();
            } else if (FaceNameToIndex(face.c_str()) >= 0) {
                RequestMouthSequenceCancel();
                applied = SetAvatarExpression(face.c_str());
            } else {
                cJSON_AddBoolToObject(root, "ok", false);
                cJSON_AddStringToObject(root, "error",
                    "Unknown face. Allowed: idle, happy, thinking, sad, "
                    "surprised, embarrassed, off.");
                ESP_LOGW(TAG, "set_avatar rejected: unknown face '%s'", face.c_str());
                return root;
            }
            cJSON_AddBoolToObject(root, "ok", applied);
            if (!applied) {
                cJSON_AddStringToObject(root, "error",
                    "Display not ready yet; retry after a moment.");
            }
            ESP_LOGI(TAG, "set_avatar: face=%s applied=%d", face.c_str(), applied);
            return root;
        });

    // Phase F: small status caption shown in front of the avatar near the
    // top of the LCD. The gateway drives it to surface its own state
    // ("きいてるよ", "考え中", "調べ中", ...). An empty string clears it.
    // Visibility is independent of the avatar, so set_avatar("off") leaves
    // any status text in place.
    mcp_server.AddTool(
        "self.display.set_status_text",
        "Set a short status caption shown over the avatar near the top of "
        "the LCD. Pass an empty string to hide it. Used to surface "
        "gateway-side state (listening, thinking, searching, ...).",
        PropertyList({Property("text", kPropertyTypeString)}),
        [this](const PropertyList& properties) -> ReturnValue {
            std::string text = properties["text"].value<std::string>();
            bool applied = SetStatusText(text.c_str());
            cJSON* root = cJSON_CreateObject();
            cJSON_AddBoolToObject(root, "ok", applied);
            if (!applied) {
                cJSON_AddStringToObject(root, "error",
                    "Display not ready yet; retry after a moment.");
            }
            ESP_LOGI(TAG, "set_status_text: text='%s' applied=%d", text.c_str(), applied);
            return root;
        });

    // Phase F: subtitle caption pinned to the bottom of the LCD. The
    // gateway drives it with what the persona is currently speaking. Wraps
    // to a few lines; an empty string clears it. Independent of the avatar
    // and of set_status_text (which lives at the top).
    mcp_server.AddTool(
        "self.display.set_subtitle",
        "Set a subtitle caption shown along the bottom of the LCD, used to "
        "display what the persona is speaking. Wraps to 2-3 lines. Pass an "
        "empty string to hide it.",
        PropertyList({Property("text", kPropertyTypeString)}),
        [this](const PropertyList& properties) -> ReturnValue {
            std::string text = properties["text"].value<std::string>();
            bool applied = SetSubtitleText(text.c_str());
            cJSON* root = cJSON_CreateObject();
            cJSON_AddBoolToObject(root, "ok", applied);
            if (!applied) {
                cJSON_AddStringToObject(root, "error",
                    "Display not ready yet; retry after a moment.");
            }
            ESP_LOGI(TAG, "set_subtitle: text='%s' applied=%d", text.c_str(), applied);
            return root;
        });

    // Phase F: small route badge in the top-right corner. The gateway
    // sends "H" while a turn is being served by the Hermes agent (vs the
    // local fast-path), and an empty string to clear it. Placed clear of
    // the top-centre status caption.
    mcp_server.AddTool(
        "self.display.set_route_badge",
        "Set a small indicator badge in the top-right corner of the LCD. "
        "The gateway sends 'H' while the Hermes agent is serving the turn. "
        "Pass an empty string to hide it.",
        PropertyList({Property("text", kPropertyTypeString)}),
        [this](const PropertyList& properties) -> ReturnValue {
            std::string text = properties["text"].value<std::string>();
            bool applied = SetRouteBadge(text.c_str());
            cJSON* root = cJSON_CreateObject();
            cJSON_AddBoolToObject(root, "ok", applied);
            if (!applied) {
                cJSON_AddStringToObject(root, "error",
                    "Display not ready yet; retry after a moment.");
            }
            ESP_LOGI(TAG, "set_route_badge: text='%s' applied=%d", text.c_str(), applied);
            return root;
        });

    // Phase 2: lip-sync. Swap the avatar to one of the mouth-only frames.
    // The shape is held until the next set_avatar / set_mouth / blink, so
    // callers should drive it from their TTS / audio level loop.
    mcp_server.AddTool(
        "self.display.set_mouth",
        "Set the avatar mouth shape. mouth must be one of: "
        "closed, half, open, e, u. Held until the next set_avatar/set_mouth, "
        "or until a blink restores the resting face.",
        PropertyList({Property("mouth", kPropertyTypeString)}),
        [this](const PropertyList& properties) -> ReturnValue {
            std::string mouth = properties["mouth"].value<std::string>();
            bool valid = (MouthShapeToIndex(mouth.c_str()) >= 0);
            cJSON* root = cJSON_CreateObject();
            cJSON_AddStringToObject(root, "mouth", mouth.c_str());
            if (!valid) {
                cJSON_AddBoolToObject(root, "ok", false);
                cJSON_AddStringToObject(root, "error",
                    "Unknown mouth. Allowed: closed, half, open, e, u.");
                ESP_LOGW(TAG, "set_mouth rejected: unknown shape '%s'", mouth.c_str());
                return root;
            }
            // Any explicit mouth set supersedes an in-flight sequence
            // (Issue #5: set_mouth("closed") doubles as the cancellation
            // path so we don't need a separate cancel_mouth_sequence
            // tool).
            RequestMouthSequenceCancel();
            bool applied = SetMouthShape(mouth.c_str());
            cJSON_AddBoolToObject(root, "ok", applied);
            if (!applied) {
                cJSON_AddStringToObject(root, "error",
                    "Display not ready yet; retry after a moment.");
            }
            ESP_LOGI(TAG, "set_mouth: mouth=%s applied=%d", mouth.c_str(), applied);
            return root;
        });

    // Phase 2: lip-sync sequence. Queue and play a list of
    // {shape, duration_ms} pairs locally so a TTS-driven caller can
    // ship one MCP call per utterance instead of N back-to-back
    // set_mouth calls (which suffer per-step WebSocket RTT jitter).
    // The MCP Property type system has no array kind, so the gateway
    // serialises `steps` to a JSON string and sends it as `steps_json`.
    // See Phase 2 lip-sync sequence playback comment block above for
    // the concurrency model and trade-offs (blink pause, atomic queue
    // replacement, final shape held).
    mcp_server.AddTool(
        "self.display.set_mouth_sequence",
        "Queue a lip-sync sequence and play it locally. steps_json must "
        "decode to a JSON array of {shape, duration_ms} objects (1..256 "
        "items, shape in {closed, half, open, e, u}, duration_ms in "
        "10..10000). Returns immediately; calling set_mouth, set_avatar, "
        "or this tool again interrupts the in-flight sequence. "
        "Autonomous blink is paused while a sequence plays and resumed "
        "when it ends (resume reads the user's most recent set_blink "
        "intent, not a snapshot). The final shape is held until the "
        "next set_mouth / set_avatar call, or until the next autonomous "
        "blink restores the resting face — the same Phase 2 trade-off "
        "that applies to set_mouth, since blink ends by repainting the "
        "full face. If the final shape must persist visually, disable "
        "blink with set_blink(false) before the sequence (or append a "
        "closed step if you just want the mouth to close at the end).",
        PropertyList({Property("steps_json", kPropertyTypeString)}),
        [this](const PropertyList& properties) -> ReturnValue {
            std::string steps_json = properties["steps_json"].value<std::string>();
            MouthSequenceEnqueueResult r = EnqueueMouthSequence(steps_json);
            cJSON* root = cJSON_CreateObject();
            cJSON_AddBoolToObject(root, "ok", r.ok);
            if (!r.ok) {
                cJSON_AddStringToObject(root, "error", r.error.c_str());
                ESP_LOGW(TAG, "set_mouth_sequence rejected: %s", r.error.c_str());
            } else {
                cJSON_AddNumberToObject(root, "queued_steps", r.queued_steps);
                cJSON_AddNumberToObject(root, "estimated_duration_ms",
                                        static_cast<double>(r.total_duration_ms));
                ESP_LOGI(TAG, "set_mouth_sequence queued %d steps (%u ms)",
                         r.queued_steps, (unsigned)r.total_duration_ms);
            }
            return root;
        });

    // Phase 2: enable/disable autonomous blinking. When enabled, a
    // background timer fires every 3-6 s (random) and runs the four-step
    // blink sequence (half -> closed -> half -> face). Also captures
    // the user's intent into blink_desired_ so that a call issued while
    // a mouth sequence is suppressing blink is honoured when the
    // sequence finishes.
    mcp_server.AddTool(
        "self.display.set_blink",
        "Enable or disable autonomous eye blinking on the avatar. "
        "When enabled, a brief blink animation runs every 3-6 seconds. "
        "If a set_mouth_sequence is currently playing, blink is paused "
        "until the sequence ends; this call still records the intent "
        "and is applied at the sequence end (or immediately if no "
        "sequence is running).",
        PropertyList({Property("enabled", kPropertyTypeBoolean)}),
        [this](const PropertyList& properties) -> ReturnValue {
            bool enabled = properties["enabled"].value<bool>();
            // blink_desired_ stays in sync with the user's intent
            // regardless of which deferral path applies, so the
            // mouth-sequence task and the avatar-fetch apply-pending
            // path both see the latest value at their respective
            // restore points.
            blink_desired_.store(enabled, std::memory_order_release);
            bool deferred_by_fetch =
                DeferAvatarBlinkIfFetching(enabled);
            bool deferred_by_mouth_seq =
                mouth_seq_active_.load(std::memory_order_acquire);
            if (!deferred_by_fetch && !deferred_by_mouth_seq) {
                if (enabled) {
                    StartBlinkTimer();
                } else {
                    StopBlinkTimer();
                }
            }
            cJSON* root = cJSON_CreateObject();
            cJSON_AddBoolToObject(root, "enabled", enabled);
            cJSON_AddBoolToObject(root, "ok", true);
            if (deferred_by_fetch || deferred_by_mouth_seq) {
                cJSON_AddBoolToObject(root, "deferred", true);
            }
            ESP_LOGI(TAG,
                     "set_blink: enabled=%d deferred_by_fetch=%d "
                     "deferred_by_mouth_seq=%d",
                     (int)enabled,
                     deferred_by_fetch ? 1 : 0,
                     deferred_by_mouth_seq ? 1 : 0);
            return root;
        });

    // Phase 7: head-touch (Si12T). Returns the latest debounced zone
    // states plus the most recent gesture event. Polled by the MCP client
    // to notice TAP/STROKE on the head without holding open a stream.
    // Phase C1 piggy-backs the latest LTR-553 proximity raw value here
    // (proximity_available / ps_raw) instead of adding a dedicated tool —
    // both values come from the same kind of poll-timer snapshot and the
    // gateway already calls this tool for sensor visibility.
    mcp_server.AddTool(
        "self.touch.get_touch_state",
        "Get the current head-touch sensor state and last gesture event "
        "(tap/stroke/idle) with its age in milliseconds. Also reports the "
        "latest proximity sensor raw value (ps_raw, 0..2047; -1 before "
        "the first sample) for hand-wave threshold calibration.",
        PropertyList(),
        [this](const PropertyList& properties) -> ReturnValue {
            cJSON* root = cJSON_CreateObject();
            cJSON_AddBoolToObject(root, "available", si12t_ok_);
            cJSON_AddBoolToObject(root, "zone0", last_zone_snapshot_[0]);
            cJSON_AddBoolToObject(root, "zone1", last_zone_snapshot_[1]);
            cJSON_AddBoolToObject(root, "zone2", last_zone_snapshot_[2]);
            cJSON_AddNumberToObject(root, "raw", last_output1_raw_);
            cJSON_AddBoolToObject(root, "proximity_available", ltr553_ok_);
            cJSON_AddNumberToObject(root, "ps_raw", last_ps_raw_);
            cJSON_AddStringToObject(root, "prox_mode",
                                    ProxModeToString(prox_mode_));
            cJSON_AddNumberToObject(root, "prox_threshold",
                                    prox_ps_threshold_);
            const char* ev = "idle";
            switch (last_event_) {
                case TouchEvent::TAP:    ev = "tap";    break;
                case TouchEvent::STROKE: ev = "stroke"; break;
                case TouchEvent::IDLE:
                default:                 ev = "idle";   break;
            }
            cJSON_AddStringToObject(root, "last_event", ev);
            int64_t age_ms = -1;
            if (last_event_us_ != 0) {
                int64_t now_us = (int64_t)esp_timer_get_time();
                age_ms = (now_us - (int64_t)last_event_us_) / 1000;
                if (age_ms < 0) age_ms = 0;
            }
            cJSON_AddNumberToObject(root, "last_event_age_ms", (double)age_ms);
            return root;
        });

    // Phase C1 follow-up (2026-06-13): runtime tuning of the proximity
    // hand-wave reaction, so changes no longer need a reflash. Both
    // arguments are required — passing only one would silently reset the
    // other to a stale schema default.
    mcp_server.AddTool(
        "self.touch.set_proximity_config",
        "Set the proximity hand-wave reaction mode and its raw PS "
        "detection threshold (0..2047; baseline ~380, hand within 10cm "
        "reads ~820+). mode must be one of: reflex (look up + happy "
        "face), listen (start a tap-equivalent listen), off (no "
        "reaction). Both values persist across reboots.",
        PropertyList({Property("mode", kPropertyTypeString),
                      Property("threshold", kPropertyTypeInteger, 0, 2047)}),
        [this](const PropertyList& properties) -> ReturnValue {
            std::string mode_str = properties["mode"].value<std::string>();
            int threshold = properties["threshold"].value<int>();
            cJSON* root = cJSON_CreateObject();
            ProxMode mode;
            if (!StringToProxMode(mode_str, &mode)) {
                cJSON_AddBoolToObject(root, "ok", false);
                cJSON_AddStringToObject(root, "error",
                    "Unknown mode. Allowed: reflex, listen, off.");
                ESP_LOGW(TAG, "set_proximity_config rejected: unknown mode '%s'",
                         mode_str.c_str());
                return root;
            }
            {
                Settings settings("stackchan_prox", true);
                settings.SetString("mode", mode_str);
                settings.SetInt("threshold", threshold);
            }
            prox_mode_ = mode;
            prox_ps_threshold_ = threshold;
            ESP_LOGI(TAG, "proximity config updated: mode=%s threshold=%d",
                     mode_str.c_str(), threshold);
            cJSON_AddBoolToObject(root, "ok", true);
            cJSON_AddStringToObject(root, "mode", mode_str.c_str());
            cJSON_AddNumberToObject(root, "threshold", threshold);
            return root;
        });

    // ---- LED tools (12x WS2812C on the StackChan base) ----
    // The strip is driven by the PY32 IO expander on its pin 13, not by
    // an ESP32 GPIO. Updates are non-latching writes into the PY32 LED
    // RAM followed by a single RefreshLeds() to strobe the strip. All
    // four tools refresh implicitly so the LLM gets WYSIWYG behaviour.
    mcp_server.AddTool(
        "self.led.set_color",
        "Set a single RGB LED on the StackChan base. There are 12 LEDs "
        "(index 0..11). r/g/b are 0..255. Updates immediately.",
        PropertyList({
            Property("index", kPropertyTypeInteger, 0, RGB_LED_COUNT - 1),
            Property("r", kPropertyTypeInteger, 0, 255),
            Property("g", kPropertyTypeInteger, 0, 255),
            Property("b", kPropertyTypeInteger, 0, 255),
        }),
        [this](const PropertyList& properties) -> ReturnValue {
            cJSON* root = cJSON_CreateObject();
            cJSON_AddBoolToObject(root, "available", rgb_ok_);
            if (!rgb_ok_) {
                cJSON_AddStringToObject(root, "error", "RGB strip not available (PY32 init failed?)");
                return root;
            }
            int index = properties["index"].value<int>();
            uint8_t r = ClampByte(properties["r"].value<int>());
            uint8_t g = ClampByte(properties["g"].value<int>());
            uint8_t b = ClampByte(properties["b"].value<int>());
            bool ok_w = io_expander_->SetLedColor((uint8_t)index, r, g, b);
            bool ok_r = ok_w ? io_expander_->RefreshLeds() : false;
            cJSON_AddBoolToObject(root, "ok", ok_w && ok_r);
            cJSON_AddNumberToObject(root, "index", index);
            ESP_LOGI(TAG, "set_led: index=%d rgb=(%u,%u,%u) ok=%d", index, r, g, b, ok_w && ok_r);
            return root;
        });

    mcp_server.AddTool(
        "self.led.set_all",
        "Set all 12 RGB LEDs on the StackChan base to the same color. "
        "r/g/b are 0..255. Updates immediately.",
        PropertyList({
            Property("r", kPropertyTypeInteger, 0, 255),
            Property("g", kPropertyTypeInteger, 0, 255),
            Property("b", kPropertyTypeInteger, 0, 255),
        }),
        [this](const PropertyList& properties) -> ReturnValue {
            cJSON* root = cJSON_CreateObject();
            cJSON_AddBoolToObject(root, "available", rgb_ok_);
            if (!rgb_ok_) {
                cJSON_AddStringToObject(root, "error", "RGB strip not available (PY32 init failed?)");
                return root;
            }
            uint8_t r = ClampByte(properties["r"].value<int>());
            uint8_t g = ClampByte(properties["g"].value<int>());
            uint8_t b = ClampByte(properties["b"].value<int>());
            uint8_t buf[RGB_LED_COUNT * 2];
            uint8_t pair[2];
            PackRgb565(r, g, b, pair);
            for (int i = 0; i < RGB_LED_COUNT; i++) {
                buf[i * 2 + 0] = pair[0];
                buf[i * 2 + 1] = pair[1];
            }
            bool ok_w = io_expander_->SetLedData(buf, sizeof(buf));
            bool ok_r = ok_w ? io_expander_->RefreshLeds() : false;
            cJSON_AddBoolToObject(root, "ok", ok_w && ok_r);
            ESP_LOGI(TAG, "set_all_leds: rgb=(%u,%u,%u) ok=%d", r, g, b, ok_w && ok_r);
            return root;
        });

    // Phase F: semantic indicator. Sets every base LED to one color so the
    // gateway can signal which brain answered (e.g. blue while Hermes is
    // responding), independent of the autonomous listening-green driven by
    // PollTouchpad (separated in time). (0,0,0) turns the strip off. Thin
    // wrapper over the same SetAllRgbLeds helper used by set_all.
    mcp_server.AddTool(
        "self.led.set_indicator",
        "Set all base RGB LEDs to one color as a status indicator. The "
        "gateway uses this to signal which brain is responding (e.g. blue "
        "while Hermes answers). r/g/b are 0..255; (0,0,0) turns it off.",
        PropertyList({
            Property("r", kPropertyTypeInteger, 0, 255),
            Property("g", kPropertyTypeInteger, 0, 255),
            Property("b", kPropertyTypeInteger, 0, 255),
        }),
        [this](const PropertyList& properties) -> ReturnValue {
            cJSON* root = cJSON_CreateObject();
            cJSON_AddBoolToObject(root, "available", rgb_ok_);
            if (!rgb_ok_) {
                cJSON_AddStringToObject(root, "error", "RGB strip not available (PY32 init failed?)");
                return root;
            }
            uint8_t r = ClampByte(properties["r"].value<int>());
            uint8_t g = ClampByte(properties["g"].value<int>());
            uint8_t b = ClampByte(properties["b"].value<int>());
            SetAllRgbLeds(r, g, b);
            // Re-arm the idle backstop: changing the indicator is LED
            // activity, so the 60 s auto-settle is measured from here.
            ScheduleIdleSettle();
            cJSON_AddBoolToObject(root, "ok", true);
            ESP_LOGI(TAG, "set_indicator: rgb=(%u,%u,%u)", r, g, b);
            return root;
        });

    // Batch set: accepts a JSON-encoded array of 12 [r,g,b] triples.
    // Single I2C burst + one refresh — use this for animations or any
    // multi-color pattern to avoid 12x round-trips. Missing trailing
    // entries are left at their previous color (PY32 RAM is sticky).
    mcp_server.AddTool(
        "self.led.set_many",
        "Set multiple RGB LEDs in one shot. 'colors' is a JSON-encoded "
        "array of [r,g,b] triples starting at index 0, e.g. "
        "\"[[255,0,0],[0,255,0],[0,0,255]]\". Up to 12 entries; extras "
        "are ignored, missing entries keep their previous color. "
        "r/g/b are 0..255. Updates immediately.",
        PropertyList({Property("colors", kPropertyTypeString)}),
        [this](const PropertyList& properties) -> ReturnValue {
            cJSON* root = cJSON_CreateObject();
            cJSON_AddBoolToObject(root, "available", rgb_ok_);
            if (!rgb_ok_) {
                cJSON_AddStringToObject(root, "error", "RGB strip not available (PY32 init failed?)");
                return root;
            }
            std::string json = properties["colors"].value<std::string>();
            cJSON* arr = cJSON_Parse(json.c_str());
            if (arr == nullptr || !cJSON_IsArray(arr)) {
                cJSON_AddBoolToObject(root, "ok", false);
                cJSON_AddStringToObject(root, "error",
                    "colors must be a JSON array of [r,g,b] triples");
                if (arr != nullptr) cJSON_Delete(arr);
                return root;
            }
            int n = cJSON_GetArraySize(arr);
            if (n > RGB_LED_COUNT) n = RGB_LED_COUNT;

            // Validate every entry FIRST and pack into a local buffer.
            // Only after the whole array is known good do we touch the
            // PY32 — that way a malformed entry at i=5 cannot leave
            // LEDs 0..4 mutated (atomic semantics, same as
            // set_mouth_sequence). cJSON_IsNumber is required because
            // valueint silently returns 0 for non-number nodes (string,
            // null, bool), so without the guard a payload like
            // [["255",0,0]] would write black and report ok=true.
            uint8_t buf[RGB_LED_COUNT * 2];   // 24 bytes, fits the cap
            bool parse_ok = true;
            for (int i = 0; i < n; i++) {
                cJSON* triple = cJSON_GetArrayItem(arr, i);
                if (!cJSON_IsArray(triple) || cJSON_GetArraySize(triple) < 3) {
                    parse_ok = false;
                    break;
                }
                cJSON* jr = cJSON_GetArrayItem(triple, 0);
                cJSON* jg = cJSON_GetArrayItem(triple, 1);
                cJSON* jb = cJSON_GetArrayItem(triple, 2);
                if (!cJSON_IsNumber(jr) || !cJSON_IsNumber(jg) || !cJSON_IsNumber(jb)) {
                    parse_ok = false;
                    break;
                }
                PackRgb565(ClampByte(jr->valueint),
                           ClampByte(jg->valueint),
                           ClampByte(jb->valueint),
                           &buf[i * 2]);
            }
            cJSON_Delete(arr);

            // Single I2C burst for the validated prefix, then one latch.
            // n=0 is treated as success (gateway schema enforces
            // minItems=1, but a direct device caller could hit this).
            bool ok_w = false, ok_r = false;
            if (parse_ok && n > 0) {
                ok_w = io_expander_->SetLedData(buf, (size_t)(n * 2));
                ok_r = ok_w ? io_expander_->RefreshLeds() : false;
            }
            bool ok = parse_ok && (n == 0 || (ok_w && ok_r));
            cJSON_AddBoolToObject(root, "ok", ok);
            cJSON_AddNumberToObject(root, "written", ok ? n : 0);
            if (!parse_ok) {
                cJSON_AddStringToObject(root, "error",
                    "Each entry must be a [r,g,b] triple of integers");
            }
            ESP_LOGI(TAG, "set_many_leds: written=%d/%d ok=%d",
                     ok ? n : 0, n, ok);
            return root;
        });

    mcp_server.AddTool(
        "self.led.clear",
        "Turn off all 12 RGB LEDs on the StackChan base. Updates immediately.",
        PropertyList(),
        [this](const PropertyList&) -> ReturnValue {
            cJSON* root = cJSON_CreateObject();
            cJSON_AddBoolToObject(root, "available", rgb_ok_);
            if (!rgb_ok_) {
                cJSON_AddStringToObject(root, "error", "RGB strip not available (PY32 init failed?)");
                return root;
            }
            uint8_t buf[RGB_LED_COUNT * 2] = {0};
            bool ok_w = io_expander_->SetLedData(buf, sizeof(buf));
            bool ok_r = ok_w ? io_expander_->RefreshLeds() : false;
            cJSON_AddBoolToObject(root, "ok", ok_w && ok_r);
            ESP_LOGI(TAG, "clear_leds: ok=%d", ok_w && ok_r);
            return root;
        });

    // ---- Generic I2C bus tools (Grove Port A) ----
    // Expose the external Port A I2C bus to the MCP client so that
    // attached M5Stack Unit modules (ENV III, ToF, gas sensor, PaHub,
    // etc.) can be driven from the gateway / host side without
    // recompiling and re-flashing per Unit. The on-board IC bus (PMIC,
    // touch, IMU, AW9523, audio codec) is on a physically separate I2C
    // controller and is NOT reachable from these tools by construction.

    mcp_server.AddTool(
        "self.i2c.scan",
        "Scan the external I2C bus on Grove Port A and return all 7-bit "
        "addresses (probe range 0x08..0x77, excluding I2C reserved "
        "ranges) that ACK a probe. Use this to discover attached "
        "M5Stack Unit modules (ENV III, ToF, gas sensor, PaHub, etc.). "
        "On-board ICs on the internal bus are NOT included (this tool "
        "operates on a physically separate bus). Returns "
        "{\"ok\":true, \"addresses\":[...]}.",
        PropertyList(),
        [this](const PropertyList&) -> ReturnValue {
            cJSON* root = cJSON_CreateObject();
            cJSON* addrs = cJSON_CreateArray();
            int found = 0;
            // Probe 0x08..0x77 (skip I2C reserved 0x00-0x07 / 0x78-0x7F).
            // 200 ms per-probe timeout matches the boot-time I2cDetect()
            // and reliably catches slower Units (RCWL-9620 etc.).
            for (uint8_t addr = 0x08; addr < 0x78; addr++) {
                esp_err_t ret = i2c_master_probe(port_a_i2c_bus_, addr, pdMS_TO_TICKS(200));
                if (ret == ESP_OK) {
                    cJSON_AddItemToArray(addrs, cJSON_CreateNumber(addr));
                    found++;
                }
            }
            cJSON_AddBoolToObject(root, "ok", true);
            cJSON_AddItemToObject(root, "addresses", addrs);
            ESP_LOGI(TAG, "i2c.scan: found %d device(s) on Port A", found);
            return root;
        });

    mcp_server.AddTool(
        "self.i2c.read",
        "Read n_bytes from an I2C device at 7-bit address `addr` on Grove "
        "Port A. `addr` is restricted to 0x08..0x77 (I2C reserved ranges "
        "excluded — matches the self.i2c.scan probe range). Use this for "
        "protocols that read the device's current register / output "
        "without a preceding write (e.g. sensors that latch a measurement "
        "from a prior command). For typical 'write register address, "
        "then read' patterns, use self.i2c.write_read instead. Returns "
        "{\"ok\":true, \"bytes\":[...]} or "
        "{\"ok\":false, \"error\":\"ESP_ERR_TIMEOUT\"} on NACK.",
        PropertyList({
            Property("addr", kPropertyTypeInteger, 0x08, 0x77),
            Property("n_bytes", kPropertyTypeInteger, 1, 256)
        }),
        [this](const PropertyList& props) -> ReturnValue {
            cJSON* root = cJSON_CreateObject();
            uint8_t addr = static_cast<uint8_t>(props["addr"].value<int>());
            int n = props["n_bytes"].value<int>();

            i2c_device_config_t cfg = {
                .dev_addr_length = I2C_ADDR_BIT_LEN_7,
                .device_address = addr,
                .scl_speed_hz = 400000,
            };
            i2c_master_dev_handle_t dev;
            esp_err_t err = i2c_master_bus_add_device(port_a_i2c_bus_, &cfg, &dev);
            if (err != ESP_OK) {
                cJSON_AddBoolToObject(root, "ok", false);
                cJSON_AddStringToObject(root, "error", esp_err_to_name(err));
                ESP_LOGW(TAG, "i2c.read addr=0x%02X add_device failed: %s",
                         addr, esp_err_to_name(err));
                return root;
            }

            std::vector<uint8_t> buf(static_cast<size_t>(n));
            err = i2c_master_receive(dev, buf.data(), buf.size(), 100);
            i2c_master_bus_rm_device(dev);

            if (err == ESP_OK) {
                cJSON* bytes = cJSON_CreateArray();
                for (uint8_t b : buf) {
                    cJSON_AddItemToArray(bytes, cJSON_CreateNumber(b));
                }
                cJSON_AddBoolToObject(root, "ok", true);
                cJSON_AddItemToObject(root, "bytes", bytes);
            } else {
                cJSON_AddBoolToObject(root, "ok", false);
                cJSON_AddStringToObject(root, "error", esp_err_to_name(err));
            }
            ESP_LOGI(TAG, "i2c.read addr=0x%02X n=%d ok=%d",
                     addr, n, err == ESP_OK);
            return root;
        });

    Property i2c_write_bytes_prop(
        "bytes", kPropertyTypeArray, kPropertyElementTypeInteger, 0, 255
    );
    i2c_write_bytes_prop.set_max_items(256);  // 対称: n_bytes の read 上限と同じ
    mcp_server.AddTool(
        "self.i2c.write",
        "Write bytes to an I2C device at 7-bit address `addr` on Grove "
        "Port A. `addr` is restricted to 0x08..0x77 (I2C reserved ranges "
        "excluded — General-call address 0x00 etc. cannot accidentally "
        "broadcast-write to all attached Units). `bytes` is an array of "
        "integers (0..255, max 256 items). This tool operates on the "
        "external Port A bus only; on-board ICs (PMIC, AW9523, touch, "
        "etc.) on the internal bus are not reachable. Returns "
        "{\"ok\":true} on ACK or "
        "{\"ok\":false, \"error\":\"ESP_ERR_TIMEOUT\"} on NACK.",
        PropertyList({
            Property("addr", kPropertyTypeInteger, 0x08, 0x77),
            i2c_write_bytes_prop
        }),
        [this](const PropertyList& props) -> ReturnValue {
            cJSON* root = cJSON_CreateObject();
            uint8_t addr = static_cast<uint8_t>(props["addr"].value<int>());
            auto bytes_int = props["bytes"].value<std::vector<int>>();

            i2c_device_config_t cfg = {
                .dev_addr_length = I2C_ADDR_BIT_LEN_7,
                .device_address = addr,
                .scl_speed_hz = 400000,
            };
            i2c_master_dev_handle_t dev;
            esp_err_t err = i2c_master_bus_add_device(port_a_i2c_bus_, &cfg, &dev);
            if (err != ESP_OK) {
                cJSON_AddBoolToObject(root, "ok", false);
                cJSON_AddStringToObject(root, "error", esp_err_to_name(err));
                ESP_LOGW(TAG, "i2c.write addr=0x%02X add_device failed: %s",
                         addr, esp_err_to_name(err));
                return root;
            }

            std::vector<uint8_t> buf;
            buf.reserve(bytes_int.size());
            for (int b : bytes_int) buf.push_back(static_cast<uint8_t>(b));

            err = i2c_master_transmit(dev, buf.data(), buf.size(), 100);
            i2c_master_bus_rm_device(dev);

            if (err == ESP_OK) {
                cJSON_AddBoolToObject(root, "ok", true);
            } else {
                cJSON_AddBoolToObject(root, "ok", false);
                cJSON_AddStringToObject(root, "error", esp_err_to_name(err));
            }
            ESP_LOGI(TAG, "i2c.write addr=0x%02X n=%d ok=%d",
                     addr, (int)buf.size(), err == ESP_OK);
            return root;
        });

    Property i2c_wr_write_bytes_prop(
        "write_bytes", kPropertyTypeArray, kPropertyElementTypeInteger, 0, 255
    );
    i2c_wr_write_bytes_prop.set_max_items(256);  // 対称: n_bytes の read 上限と同じ
    mcp_server.AddTool(
        "self.i2c.write_read",
        "Write `write_bytes` to an I2C device at 7-bit address `addr` on "
        "Grove Port A, then read n_bytes back in a single transaction "
        "(Repeated Start). `addr` is restricted to 0x08..0x77 (I2C "
        "reserved ranges excluded). `write_bytes` is an array of "
        "integers (0..255, max 256 items). This is the common 'set "
        "register pointer, then read' pattern: pass write_bytes=[reg_addr] "
        "to read from a specific register. Returns "
        "{\"ok\":true, \"bytes\":[...]} or "
        "{\"ok\":false, \"error\":\"...\"} on failure.",
        PropertyList({
            Property("addr", kPropertyTypeInteger, 0x08, 0x77),
            i2c_wr_write_bytes_prop,
            Property("n_bytes", kPropertyTypeInteger, 1, 256)
        }),
        [this](const PropertyList& props) -> ReturnValue {
            cJSON* root = cJSON_CreateObject();
            uint8_t addr = static_cast<uint8_t>(props["addr"].value<int>());
            auto write_bytes_int = props["write_bytes"].value<std::vector<int>>();
            int n = props["n_bytes"].value<int>();

            i2c_device_config_t cfg = {
                .dev_addr_length = I2C_ADDR_BIT_LEN_7,
                .device_address = addr,
                .scl_speed_hz = 400000,
            };
            i2c_master_dev_handle_t dev;
            esp_err_t err = i2c_master_bus_add_device(port_a_i2c_bus_, &cfg, &dev);
            if (err != ESP_OK) {
                cJSON_AddBoolToObject(root, "ok", false);
                cJSON_AddStringToObject(root, "error", esp_err_to_name(err));
                ESP_LOGW(TAG, "i2c.write_read addr=0x%02X add_device failed: %s",
                         addr, esp_err_to_name(err));
                return root;
            }

            std::vector<uint8_t> write_buf;
            write_buf.reserve(write_bytes_int.size());
            for (int b : write_bytes_int) write_buf.push_back(static_cast<uint8_t>(b));

            std::vector<uint8_t> read_buf(static_cast<size_t>(n));
            err = i2c_master_transmit_receive(dev,
                                               write_buf.data(), write_buf.size(),
                                               read_buf.data(), read_buf.size(),
                                               100);
            i2c_master_bus_rm_device(dev);

            if (err == ESP_OK) {
                cJSON* bytes = cJSON_CreateArray();
                for (uint8_t b : read_buf) {
                    cJSON_AddItemToArray(bytes, cJSON_CreateNumber(b));
                }
                cJSON_AddBoolToObject(root, "ok", true);
                cJSON_AddItemToObject(root, "bytes", bytes);
            } else {
                cJSON_AddBoolToObject(root, "ok", false);
                cJSON_AddStringToObject(root, "error", esp_err_to_name(err));
            }
            ESP_LOGI(TAG, "i2c.write_read addr=0x%02X w=%d r=%d ok=%d",
                     addr, (int)write_buf.size(), n, err == ESP_OK);
            return root;
        });

    // ---- Generic Port B WS2812 strip tools ----
    // Expose the CoreS3 Port B digital output (GPIO 9) as a generic
    // WS2812-compatible strip driver. This is independent from self.led.*,
    // which drives the 12-LED base strip through the PY32 I2C path.

    mcp_server.AddTool(
        "self.port_b.ws2812.init",
        "Initialize a WS2812-compatible LED strip connected to Port B "
        "(CoreS3 HY2.0-4P digital OUTPUT, GPIO 9). led_count is the "
        "number of LEDs in the strip (1..256). This allocates the "
        "ESP-IDF led_strip RMT backend and must succeed before calling "
        "self.port_b.ws2812.set_pixel, set_strip, refresh, or clear. "
        "Repeated calls with the same led_count are no-ops; a different "
        "led_count tears down and rebuilds the strip handle. Returns "
        "{\"available\":true,\"ok\":true,\"led_count\":N} on success or "
        "{\"available\":false,\"ok\":false,\"led_count\":N,"
        "\"error\":\"ESP_ERR_...\"} on failure. The strip protocol is "
        "3.3 V CMOS data on GPIO 9; most modern WS2812B-V5/B2 strips "
        "tolerate this, while older strict 5 V V_IH variants may need "
        "an external level shifter.",
        PropertyList({
            Property("led_count", kPropertyTypeInteger, 1, PORT_B_WS2812_MAX_LEDS)
        }),
        [this](const PropertyList& props) -> ReturnValue {
            cJSON* root = cJSON_CreateObject();
            uint16_t led_count = static_cast<uint16_t>(props["led_count"].value<int>());
            esp_err_t err = InitPortBWs2812(led_count);
            bool ok = (err == ESP_OK);
            cJSON_AddBoolToObject(root, "available", ok);
            cJSON_AddBoolToObject(root, "ok", ok);
            cJSON_AddNumberToObject(root, "led_count", led_count);
            if (!ok) {
                cJSON_AddStringToObject(root, "error", esp_err_to_name(err));
            }
            ESP_LOGI(TAG, "port_b.ws2812.init led_count=%u ok=%d",
                     (unsigned)led_count, ok ? 1 : 0);
            return root;
        });

    mcp_server.AddTool(
        "self.port_b.ws2812.set_pixel",
        "Set one LED in the Port B WS2812 strip buffer. Call "
        "self.port_b.ws2812.init first; until init succeeds this returns "
        "{\"available\":false,\"ok\":false}. index is 0..255, but the "
        "effective range is 0..(led_count-1); out-of-range requests "
        "return ok=false with error=\"index out of range\". r, g, and b "
        "are 0..255. By default the color is buffered only; pass "
        "refresh=true to immediately latch it to the strip, or call "
        "self.port_b.ws2812.refresh after several buffered updates. "
        "Runtime led_strip failures return ok=false with error. Port B "
        "outputs 3.3 V CMOS data on GPIO 9; older strict 5 V WS2812 "
        "variants may require a level shifter.",
        PropertyList({
            Property("index", kPropertyTypeInteger, 0, PORT_B_WS2812_MAX_LEDS - 1),
            Property("r", kPropertyTypeInteger, 0, 255),
            Property("g", kPropertyTypeInteger, 0, 255),
            Property("b", kPropertyTypeInteger, 0, 255),
            Property("refresh", kPropertyTypeBoolean, false)
        }),
        [this](const PropertyList& props) -> ReturnValue {
            cJSON* root = cJSON_CreateObject();
            cJSON_AddBoolToObject(root, "available", ws2812_ok_);
            if (!ws2812_ok_ || ws2812_handle_ == nullptr) {
                cJSON_AddBoolToObject(root, "ok", false);
                cJSON_AddStringToObject(root, "error",
                                        "Port B WS2812 strip not initialized.");
                return root;
            }
            int index = props["index"].value<int>();
            if (index >= ws2812_led_count_) {
                cJSON_AddBoolToObject(root, "ok", false);
                cJSON_AddStringToObject(root, "error", "index out of range");
                ESP_LOGW(TAG, "port_b.ws2812.set_pixel index=%d out of range (led_count=%u)",
                         index, (unsigned)ws2812_led_count_);
                return root;
            }
            uint8_t r = ClampByte(props["r"].value<int>());
            uint8_t g = ClampByte(props["g"].value<int>());
            uint8_t b = ClampByte(props["b"].value<int>());
            bool refresh = props["refresh"].value<bool>();
            esp_err_t err = led_strip_set_pixel(ws2812_handle_, index, r, g, b);
            if (err == ESP_OK && refresh) {
                err = led_strip_refresh(ws2812_handle_);
            }
            bool ok = (err == ESP_OK);
            cJSON_AddBoolToObject(root, "ok", ok);
            if (!ok) {
                cJSON_AddStringToObject(root, "error", esp_err_to_name(err));
            }
            ESP_LOGI(TAG, "port_b.ws2812.set_pixel index=%d rgb=(%u,%u,%u) refresh=%d ok=%d",
                     index, r, g, b, refresh ? 1 : 0, ok ? 1 : 0);
            return root;
        });

    mcp_server.AddTool(
        "self.port_b.ws2812.set_strip",
        "Set multiple LEDs in the Port B WS2812 strip and refresh "
        "immediately. Call self.port_b.ws2812.init first; until init "
        "succeeds this returns {\"available\":false,\"ok\":false}. "
        "colors is a JSON-encoded array of [r,g,b] integer triples, "
        "for example \"[[255,0,0],[0,255,0],[0,0,255]]\". Entries are "
        "applied from LED index 0; up to led_count entries are written, "
        "extras are ignored, and missing trailing entries preserve the "
        "previous buffered values. The payload is validate-then-write: "
        "a malformed entry leaves the strip buffer unchanged. This tool "
        "auto-refreshes and is the preferred path for animation frames. "
        "Runtime led_strip failures return ok=false with error. Port B "
        "outputs 3.3 V CMOS data on GPIO 9; older strict 5 V WS2812 "
        "variants may require a level shifter.",
        PropertyList({Property("colors", kPropertyTypeString)}),
        [this](const PropertyList& props) -> ReturnValue {
            cJSON* root = cJSON_CreateObject();
            cJSON_AddBoolToObject(root, "available", ws2812_ok_);
            if (!ws2812_ok_ || ws2812_handle_ == nullptr) {
                cJSON_AddBoolToObject(root, "ok", false);
                cJSON_AddNumberToObject(root, "written", 0);
                cJSON_AddStringToObject(root, "error",
                                        "Port B WS2812 strip not initialized.");
                return root;
            }

            std::string json = props["colors"].value<std::string>();
            cJSON* arr = cJSON_Parse(json.c_str());
            if (arr == nullptr || !cJSON_IsArray(arr)) {
                cJSON_AddBoolToObject(root, "ok", false);
                cJSON_AddNumberToObject(root, "written", 0);
                cJSON_AddStringToObject(root, "error",
                                        "colors must be a JSON array of [r,g,b] triples");
                if (arr != nullptr) cJSON_Delete(arr);
                return root;
            }

            int n = cJSON_GetArraySize(arr);
            if (n > ws2812_led_count_) n = ws2812_led_count_;
            std::vector<uint8_t> rgb;
            rgb.reserve(static_cast<size_t>(n) * 3);
            bool parse_ok = true;
            for (int i = 0; i < n; i++) {
                cJSON* triple = cJSON_GetArrayItem(arr, i);
                if (!cJSON_IsArray(triple) || cJSON_GetArraySize(triple) != 3) {
                    parse_ok = false;
                    break;
                }
                uint8_t r = 0, g = 0, b = 0;
                if (!JsonByte(cJSON_GetArrayItem(triple, 0), &r) ||
                    !JsonByte(cJSON_GetArrayItem(triple, 1), &g) ||
                    !JsonByte(cJSON_GetArrayItem(triple, 2), &b)) {
                    parse_ok = false;
                    break;
                }
                rgb.push_back(r);
                rgb.push_back(g);
                rgb.push_back(b);
            }
            cJSON_Delete(arr);

            if (!parse_ok) {
                cJSON_AddBoolToObject(root, "ok", false);
                cJSON_AddNumberToObject(root, "written", 0);
                cJSON_AddStringToObject(root, "error",
                                        "Each entry must be a [r,g,b] triple of integers 0..255");
                ESP_LOGW(TAG, "port_b.ws2812.set_strip rejected malformed colors payload");
                return root;
            }

            esp_err_t err = ESP_OK;
            for (int i = 0; i < n; i++) {
                size_t offset = static_cast<size_t>(i) * 3;
                err = led_strip_set_pixel(ws2812_handle_, i,
                                          rgb[offset + 0],
                                          rgb[offset + 1],
                                          rgb[offset + 2]);
                if (err != ESP_OK) {
                    break;
                }
            }
            if (err == ESP_OK) {
                err = led_strip_refresh(ws2812_handle_);
            }

            bool ok = (err == ESP_OK);
            cJSON_AddBoolToObject(root, "ok", ok);
            cJSON_AddNumberToObject(root, "written", ok ? n : 0);
            if (!ok) {
                cJSON_AddStringToObject(root, "error", esp_err_to_name(err));
            }
            ESP_LOGI(TAG, "port_b.ws2812.set_strip written=%d ok=%d",
                     ok ? n : 0, ok ? 1 : 0);
            return root;
        });

    mcp_server.AddTool(
        "self.port_b.ws2812.refresh",
        "Refresh the Port B WS2812 strip, latching the current buffered "
        "colors out on CoreS3 HY2.0-4P digital OUTPUT GPIO 9. Call "
        "self.port_b.ws2812.init first; until init succeeds this returns "
        "{\"available\":false,\"ok\":false}. Use this after one or more "
        "self.port_b.ws2812.set_pixel calls made with refresh=false. "
        "Runtime led_strip failures return ok=false with error. Port B "
        "outputs 3.3 V CMOS data; older strict 5 V WS2812 variants may "
        "require a level shifter.",
        PropertyList(),
        [this](const PropertyList&) -> ReturnValue {
            cJSON* root = cJSON_CreateObject();
            cJSON_AddBoolToObject(root, "available", ws2812_ok_);
            if (!ws2812_ok_ || ws2812_handle_ == nullptr) {
                cJSON_AddBoolToObject(root, "ok", false);
                cJSON_AddStringToObject(root, "error",
                                        "Port B WS2812 strip not initialized.");
                return root;
            }
            esp_err_t err = led_strip_refresh(ws2812_handle_);
            bool ok = (err == ESP_OK);
            cJSON_AddBoolToObject(root, "ok", ok);
            if (!ok) {
                cJSON_AddStringToObject(root, "error", esp_err_to_name(err));
            }
            ESP_LOGI(TAG, "port_b.ws2812.refresh ok=%d", ok ? 1 : 0);
            return root;
        });

    mcp_server.AddTool(
        "self.port_b.ws2812.clear",
        "Turn off every LED in the Port B WS2812 strip and refresh "
        "immediately on CoreS3 HY2.0-4P digital OUTPUT GPIO 9. Call "
        "self.port_b.ws2812.init first; until init succeeds this returns "
        "{\"available\":false,\"ok\":false}. This is equivalent to "
        "self.port_b.ws2812.set_strip with an all-zero array of length "
        "led_count, and it clears the driver's sticky per-pixel buffer. "
        "Runtime led_strip failures return ok=false with error. Port B "
        "outputs 3.3 V CMOS data; older strict 5 V WS2812 variants may "
        "require a level shifter.",
        PropertyList(),
        [this](const PropertyList&) -> ReturnValue {
            cJSON* root = cJSON_CreateObject();
            cJSON_AddBoolToObject(root, "available", ws2812_ok_);
            if (!ws2812_ok_ || ws2812_handle_ == nullptr) {
                cJSON_AddBoolToObject(root, "ok", false);
                cJSON_AddStringToObject(root, "error",
                                        "Port B WS2812 strip not initialized.");
                return root;
            }
            esp_err_t err = led_strip_clear(ws2812_handle_);
            bool ok = (err == ESP_OK);
            cJSON_AddBoolToObject(root, "ok", ok);
            if (!ok) {
                cJSON_AddStringToObject(root, "error", esp_err_to_name(err));
            }
            ESP_LOGI(TAG, "port_b.ws2812.clear ok=%d", ok ? 1 : 0);
            return root;
        });

    ESP_LOGI(TAG, "StackChan MCP tools registered");

}
