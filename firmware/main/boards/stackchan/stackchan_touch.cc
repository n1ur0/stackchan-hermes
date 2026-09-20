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

void StackChanBoard::PollTouchpad() {

    static bool was_touched = false;
    static int64_t touch_start_time = 0;
    static int64_t last_release_ms = 0;       // デバウンス用 (= 直前 release 時刻)
    static int64_t listening_started_ms = 0;  // タイムアウト用 (= listening 突入時刻)
    static bool was_listening = false;        // listening 突入のエッジ検出
    static bool speech_seen = false;          // この listen 中に VAD が一度でも発話検知したか
    static int64_t last_voice_ms = 0;         // 最後に VAD が発話を報告した時刻
    const int64_t TOUCH_THRESHOLD_MS = 500;   // 触摸时长阈值，超过500ms视为长按
    const int64_t DEBOUNCE_MS = 300;          // 直前 release から N ms 以内の press は無視
    const int64_t LISTEN_TIMEOUT_MS = 30000;  // listening 状態に N ms 以上滞在で auto stop
    const int64_t VAD_WARMUP_MS = 800;        // listen 突入直後はポップアップ音の自己拾音で
                                              // VAD が立つことがあるため判定を保留する
    const int64_t VAD_SILENCE_STOP_MS = 1200; // 発話検知後、 無音が N ms 続いたら auto stop

    auto& app = Application::GetInstance();
    int64_t now_ms = esp_timer_get_time() / 1000;

    // --- listening 状態の上界 (タイムアウト) 管理 ---
    // 状態遷移のエッジ検出で突入時刻を記録、 滞在時間が LISTEN_TIMEOUT_MS を
    // 超えたら StopListening を自動発火する。 タッチ忘れ放置で listen が
    // 無限持続するのを防ぐ。 StopListening 後は listening_started_ms を 0 に
    // 戻して再発火を抑止 (次に listening 突入したら再セット)。
    bool is_listening = (app.GetDeviceState() == kDeviceStateListening);
    if (is_listening && !was_listening) {
        listening_started_ms = now_ms;
        speech_seen = false;
        last_voice_ms = 0;
        ESP_LOGI(TAG, "Listening entered at %d ms (timeout in %d ms)",
                 (int)now_ms, (int)LISTEN_TIMEOUT_MS);
        // 録音開始フィードバック (= 全 LED 緑点灯)。 device state が
        // Listening に入る全経路 (タッチ / ウェイクワード / ダッシュボード
        // の StartListening) がこのエッジを通るため、 ここで一括点灯する。
        // タッチ release 経路 (2577) でも同色で点灯するが、 二重でも無害。
        // 消灯は既存の 3 経路 (timeout / VAD 無音 / 再タップ) でカバー。
        SetAllRgbLeds(0, 32, 0);
        // Listening 突入は「顔/LED を変える操作」なので idle backstop を
        // 再武装する。全 listening 経路 (タッチ/ウェイクワード/ダッシュ
        // ボード) がこのエッジを通るためここで一括。
        ScheduleIdleSettle();
    }
    was_listening = is_listening;
    if (is_listening && listening_started_ms != 0 &&
        (now_ms - listening_started_ms) > LISTEN_TIMEOUT_MS) {
        ESP_LOGI(TAG, "Listening timeout reached (%d ms) -> StopListening",
                 (int)(now_ms - listening_started_ms));
        SetAllRgbLeds(0, 0, 0);
        app.StopListening();
        listening_started_ms = 0;
    }

    // --- VAD 無音検出による auto stop (再タップ不要化) ---
    // AFE の VAD (AudioService::voice_detected_) をポーリングし、 発話を
    // 一度でも検知した後に無音が VAD_SILENCE_STOP_MS 続いたら自動送信する。
    // 発話を一度も検知していない間は止めない (VAD が不感だった場合に
    // ユーザーの発話を切り捨てないための保険。 上の 30s タイムアウトが
    // backstop として残る)。
    if (is_listening && listening_started_ms != 0 &&
        (now_ms - listening_started_ms) > VAD_WARMUP_MS) {
        if (app.IsVoiceDetected()) {
            speech_seen = true;
            last_voice_ms = now_ms;
        } else if (speech_seen &&
                   (now_ms - last_voice_ms) > VAD_SILENCE_STOP_MS) {
            ESP_LOGI(TAG, "VAD silence auto-stop (%d ms after last voice)",
                     (int)(now_ms - last_voice_ms));
            SetAllRgbLeds(0, 0, 0);
            app.StopListening();
            listening_started_ms = 0;
        }
    }

    ft6336_->UpdateTouchPoint();
    auto& touch_point = ft6336_->GetTouchPoint();

    // 检测触摸开始
    if (touch_point.num > 0 && !was_touched) {
        // デバウンス: 直前 release から DEBOUNCE_MS 以内の press は無視。
        // FT6336 のチャタリングや「タッチした直後にもう一度触れてしまう」
        // 連打事故を防止。
        if (last_release_ms != 0 && (now_ms - last_release_ms) < DEBOUNCE_MS) {
            // was_touched は更新しない。 次の poll でも press 判定を再評価
            // するが、 デバウンス期間を超えれば通常 press として処理される。
            return;
        }
        was_touched = true;
        touch_start_time = now_ms;
        // タッチ瞬時の PlaySound 直接呼び出しは行わない。 直後に
        // StartListening → EnableVoiceProcessing(true) → ResetDecoder で
        // playback queue がクリアされて音が消えるため。 代わりに
        // Application::StartListening 側で play_popup_on_listening_ flag を
        // 立てて、 HandleStateChangedEvent の Listening 分岐後半 (ResetDecoder
        // の後) で OGG_POPUP を鳴らす経路に乗せる (= xiaozhi 標準の WakeWord
        // 経路と同じ仕組み)。
    }
    // 检测触摸释放
    else if (touch_point.num == 0 && was_touched) {
        was_touched = false;
        int64_t touch_duration = now_ms - touch_start_time;
        last_release_ms = now_ms;

        // 只有短触才触发
        if (touch_duration < TOUCH_THRESHOLD_MS) {
            if (app.GetDeviceState() == kDeviceStateStarting) {
                EnterWifiConfigMode();
                return;
            }
            // kDeviceStateAudioTesting は WiFi config 完了直後の audio test
            // モードに居る状態。 ここから WifiConfiguring に戻る経路は
            // ToggleChatState() しか持っていない (= HandleStartListeningEvent
            // は AudioTesting を扱わない)。 StartListening にだけ分岐すると
            // タッチで設定モードに復帰できなくなるので、 AudioTesting だけ
            // は従来通り ToggleChatState() に流して状態機械任せにする。
            if (app.GetDeviceState() == kDeviceStateAudioTesting) {
                app.ToggleChatState();
                return;
            }
            // listening 中の2回目タッチは Application::HandleToggleChatEvent
            // の既定経路 (CloseAudioChannel = WS 切断 → gateway の recording
            // slot が aborted_mid_capture として buffer 破棄) ではなく
            // StopListening (= SendStopListening) に分岐させる。これで
            // device-driven audio capture push 経路 (gateway 側
            // audio_input_hook) が listen.stop を受けて buffer を Ogg 化 +
            // 外部 hook へ POST できる。Vessel UX として「タッチで listen
            // 開始 → 発話 → タッチで送信」を成立させるための fork 専用分岐。
            if (app.GetDeviceState() == kDeviceStateListening) {
                // 録音終了のフィードバック (= 全 LED 消灯)。 デバッグ目的、
                // MCP self.led.set_* 経由で上書き可能。
                SetAllRgbLeds(0, 0, 0);
                app.StopListening();
            } else {
                // listening 開始は ToggleChatState ではなく StartListening
                // を使う。 ToggleChatState 経由は SetListeningMode に
                // GetDefaultListeningMode() (= AutoStop) を渡すため、
                // ペルソナ発話終了 (tts.stop) の Schedule 内で device が
                // 自動的に Listening 状態に再復帰してしまい (= xiaozhi の
                // 連続会話モデル、 application.cc:565)、 「タッチ駆動」 が
                // 破綻する (= 次のタッチが listen.stop 経路に入って即送信)。
                // StartListening 経由は HandleStartListeningEvent で
                // SetListeningMode(ManualStop) を強制するので、 tts.stop 後
                // は Idle に留まり、 次のタッチで明示的に listen 開始する
                // Vessel UX が成立する。 Idle 以外 (Speaking 等) でも
                // HandleStartListeningEvent が AbortSpeaking → ManualStop で
                // 適切に処理する。
                // 録音開始想定のフィードバック (= 全 LED 緑点灯、 控えめ
                // な輝度)。 実際の listen 起動は StartListening 経由で
                // 非同期処理。 タッチが取れたかどうかの体感を優先。
                SetAllRgbLeds(0, 32, 0);
                app.StartListening();
            }
        }
    }

}

void StackChanBoard::InitializeFt6336TouchPad() {

    ESP_LOGI(TAG, "Init FT6336");
    ft6336_ = new Ft6336(i2c_bus_, 0x38);
    
    // 创建定时器，20ms 间隔
    esp_timer_create_args_t timer_args = {
        .callback = [](void* arg) {
            StackChanBoard* board = (StackChanBoard*)arg;
            board->PollTouchpad();
        },
        .arg = this,
        .dispatch_method = ESP_TIMER_TASK,
        .name = "touchpad_timer",
        .skip_unhandled_events = true,
    };
    
    ESP_ERROR_CHECK(esp_timer_create(&timer_args, &touchpad_timer_));
    ESP_ERROR_CHECK(esp_timer_start_periodic(touchpad_timer_, 20 * 1000));

}

void StackChanBoard::TouchRevertCb(void* arg) {

    StackChanBoard* self = static_cast<StackChanBoard*>(arg);
    self->SetAvatarExpressionIfActive("idle");
    // Also recenter the head: HandleProximity / HandleStroke tilt the
    // head up via this shared revert path, so resetting only the face
    // would leave stack-chan staring upward indefinitely. Returns to the
    // NVS-resolved neutral pose (set_neutral_pose), default BOOT_INIT_*.
    self->WriteHeadAngles(self->neutral_yaw_, self->neutral_pitch_);

}

void StackChanBoard::ScheduleIdleRevert() {

    if (touch_revert_timer_ == nullptr) {
        esp_timer_create_args_t args = {
            .callback = &StackChanBoard::TouchRevertCb,
            .arg = this,
            .dispatch_method = ESP_TIMER_TASK,
            .name = "touch_revert",
            .skip_unhandled_events = true,
        };
        ESP_ERROR_CHECK(esp_timer_create(&args, &touch_revert_timer_));
    }
    esp_timer_stop(touch_revert_timer_);  // ok if not running
    esp_timer_start_once(touch_revert_timer_,
                         (uint64_t)REACTION_HOLD_MS * 1000);

}

void StackChanBoard::IdleSettleCb(void* arg) {

    StackChanBoard* self = static_cast<StackChanBoard*>(arg);
    self->OnIdleSettle();

}

void StackChanBoard::OnIdleSettle() {

    // Only re-engage servo torque if the head is meaningfully off-center;
    // otherwise a settled head would needlessly re-energize every 60 s.
    if (servo_ok_ && motion_driver_ != nullptr) {
        int yaw_delta = std::abs(neutral_yaw_ -
            static_cast<int>(motion_driver_->GetYawDeg()));
        int pitch_delta = std::abs(neutral_pitch_ -
            static_cast<int>(motion_driver_->GetPitchDeg()));
        if (std::max(yaw_delta, pitch_delta) > IDLE_SETTLE_DEADBAND_DEG) {
            WriteHeadAngles(neutral_yaw_, neutral_pitch_);
        }
    }
    SetAvatarExpressionIfActive("idle");
    SetAllRgbLeds(0, 0, 0);

}

void StackChanBoard::ScheduleIdleSettle() {

    if (idle_settle_timer_ == nullptr) {
        esp_timer_create_args_t args = {
            .callback = &StackChanBoard::IdleSettleCb,
            .arg = this,
            .dispatch_method = ESP_TIMER_TASK,
            .name = "idle_settle",
            .skip_unhandled_events = true,
        };
        ESP_ERROR_CHECK(esp_timer_create(&args, &idle_settle_timer_));
    }
    esp_timer_stop(idle_settle_timer_);  // ok if not running
    esp_timer_start_once(idle_settle_timer_,
                         (uint64_t)IDLE_SETTLE_MS * 1000);

}

char StackChanBoard::Si12tChLevelChar(uint8_t raw, int ch) {

    uint8_t v = (raw >> (ch * 2)) & 0x3;
    return "0LMH"[v];

}

void StackChanBoard::LogTouchEvent(const char* event_name, uint64_t duration_ms) {

    ESP_LOGI(TAG,
             "touch event: %s start_zones=%d%d%d start_raw=0x%02X ch=%c%c%c%c "
             "release_raw=0x%02X duration=%u ms",
             event_name,
             press_start_zones_[0], press_start_zones_[1], press_start_zones_[2],
             press_start_output1_raw_,
             Si12tChLevelChar(press_start_output1_raw_, 0),
             Si12tChLevelChar(press_start_output1_raw_, 1),
             Si12tChLevelChar(press_start_output1_raw_, 2),
             Si12tChLevelChar(press_start_output1_raw_, 3),
             last_output1_raw_,
             (unsigned)duration_ms);

}

void StackChanBoard::HandleTap(uint64_t duration_ms) {

    LogTouchEvent("TAP", duration_ms);
    last_event_ = TouchEvent::TAP;
    last_event_us_ = esp_timer_get_time();
    // Use the IfActive variant so a tap during set_avatar("off") does
    // not pop the avatar back over the WiFi config / settings screens.
    SetAvatarExpressionIfActive("surprised");
    ScheduleIdleRevert();
    ScheduleIdleSettle();
    Application::GetInstance().SendStackChanEvent("touch", "tap", duration_ms);

}

void StackChanBoard::HandleStroke(uint64_t duration_ms) {

    LogTouchEvent("STROKE", duration_ms);
    last_event_ = TouchEvent::STROKE;
    last_event_us_ = esp_timer_get_time();
    SetAvatarExpressionIfActive("embarrassed");
    StartServoWobble();
    ScheduleIdleRevert();
    ScheduleIdleSettle();
    Application::GetInstance().SendStackChanEvent("touch", "stroke", duration_ms);

}

void StackChanBoard::TouchPollCb(void* arg) {

    StackChanBoard* self = static_cast<StackChanBoard*>(arg);
    self->TouchPollTick();

}

void StackChanBoard::TouchPollTick() {

    if (!si12t_ok_ || si12t_ == nullptr) {
        return;
    }
    Si12T::TouchState s = si12t_->ReadTouchState();
    if (!s.ok) {
        return;
    }
    // Snapshot for MCP visibility.
    last_output1_raw_ = s.output1_raw;
    last_zone_snapshot_[0] = s.zone[0];
    last_zone_snapshot_[1] = s.zone[1];
    last_zone_snapshot_[2] = s.zone[2];

    bool any_pressed = s.zone[0] || s.zone[1] || s.zone[2];

    // Asymmetric debounce:
    //   press   confirm = 2 samples ( 200 ms) — fast tap detection
    //   release confirm = 4 samples ( 400 ms) — bridges Si12T recalibration
    //                                            and finger-glide gaps that
    //                                            otherwise cut a stroke
    //                                            short and mis-classify it
    //                                            as a tap.
    // Keeping a press "sticky" through brief no-press blips is essential
    // for the stroke gesture to reach STROKE_MIN_MS.
    if (any_pressed == touch_pressed_pending_) {
        touch_pending_count_++;
    } else {
        touch_pending_count_ = 1;
        touch_pressed_pending_ = any_pressed;
    }
    const int needed = touch_pressed_pending_ ? 2 : 4;
    if (touch_pending_count_ < needed) {
        return;  // not yet debounced
    }

    bool now = touch_pressed_pending_;
    if (now == touch_pressed_prev_) {
        return;  // no edge
    }

    uint64_t now_us = esp_timer_get_time();

    if (now) {
        // Rising edge. Capture the sensor state for the falling-edge
        // log either way — without this, a press that begins during
        // the post-reaction cooldown and is held until the cooldown
        // expires would log the previous touch's start_zones /
        // start_raw on its falling edge, exactly the
        // repeated-touch / noise-overlap scenario this logging is
        // meant to clarify.
        press_start_zones_[0] = s.zone[0];
        press_start_zones_[1] = s.zone[1];
        press_start_zones_[2] = s.zone[2];
        press_start_output1_raw_ = s.output1_raw;
        if (now_us < cooldown_until_us_) {
            // Suppress press event while in post-reaction cooldown.
            touch_pressed_prev_ = now;
            touch_press_start_us_ = now_us;
            return;
        }
        touch_pressed_prev_ = true;
        touch_press_start_us_ = now_us;
    } else {
        // Falling edge: classify by hold duration.
        touch_pressed_prev_ = false;
        uint64_t duration_ms = (now_us - touch_press_start_us_) / 1000ULL;
        if (now_us < cooldown_until_us_) {
            // We were in cooldown when pressed — drop the release event too.
            return;
        }
        if (duration_ms >= STROKE_MIN_MS) {
            HandleStroke(duration_ms);
        } else {
            // Treat the 400-600 ms grey zone as TAP.
            HandleTap(duration_ms);
        }
        cooldown_until_us_ = now_us + (uint64_t)COOLDOWN_MS * 1000ULL;
    }

}

void StackChanBoard::InitializeSi12tTouch() {

    ESP_LOGI(TAG, "Init Si12T head-touch sensor (I2C addr 0x%02X)", Si12T::DEFAULT_ADDR);
    si12t_ = std::unique_ptr<Si12T>(new Si12T(i2c_bus_));
    si12t_ok_ = si12t_->Begin();
    if (!si12t_ok_) {
        ESP_LOGW(TAG, "Si12T not detected; head-touch disabled (other features unaffected)");
        si12t_.reset();
        return;
    }

    esp_timer_create_args_t poll_args = {
        .callback = &StackChanBoard::TouchPollCb,
        .arg = this,
        .dispatch_method = ESP_TIMER_TASK,
        .name = "touch_poll",
        .skip_unhandled_events = true,
    };
    ESP_ERROR_CHECK(esp_timer_create(&poll_args, &touch_poll_timer_));
    ESP_ERROR_CHECK(esp_timer_start_periodic(touch_poll_timer_,
                                             (uint64_t)TOUCH_POLL_MS * 1000));
    ESP_LOGI(TAG, "Si12T touch poll started (%d ms interval)", TOUCH_POLL_MS);

}

const char* StackChanBoard::ProxModeToString(ProxMode mode) {

    switch (mode) {
        case ProxMode::Reflex: return "reflex";
        case ProxMode::Listen: return "listen";
        case ProxMode::Off:
        default:               return "off";
    }

}

bool StackChanBoard::StringToProxMode(const std::string& s, ProxMode* out) {

    if (s == "reflex") { *out = ProxMode::Reflex; return true; }
    if (s == "listen") { *out = ProxMode::Listen; return true; }
    if (s == "off")    { *out = ProxMode::Off;    return true; }
    return false;

}

void StackChanBoard::HandleProximity(int ps_raw) {

    switch (prox_mode_) {
        case ProxMode::Listen: {
            auto& app = Application::GetInstance();
            if (app.GetDeviceState() == kDeviceStateListening) {
                ESP_LOGI(TAG, "proximity event: HAND ps_raw=%d -> stop listening (send)",
                         ps_raw);
                SetAllRgbLeds(0, 0, 0);   // capture-complete feedback
                app.StopListening();
            } else {
                ESP_LOGI(TAG, "proximity event: HAND ps_raw=%d (threshold=%d) -> start listening",
                         ps_raw, prox_ps_threshold_);
                SetAllRgbLeds(0, 32, 0);  // capture-start feedback (dim green)
                app.StartListening();
            }
            break;
        }
        case ProxMode::Reflex:
            ESP_LOGI(TAG, "proximity event: HAND ps_raw=%d (threshold=%d) -> look up",
                     ps_raw, prox_ps_threshold_);
            WriteHeadAngles(PROX_REACT_YAW_DEG, PROX_REACT_PITCH_DEG);
            SetAvatarExpressionIfActive("happy");
            ScheduleIdleRevert();
            ScheduleIdleSettle();
            break;
        case ProxMode::Off:
        default:
            // Unreachable: ProximityPollTick gates on prox_mode_ != Off.
            break;
    }

}

void StackChanBoard::ProximityPollCb(void* arg) {

    StackChanBoard* self = static_cast<StackChanBoard*>(arg);
    self->ProximityPollTick();

}

void StackChanBoard::ProximityPollTick() {

    if (!ltr553_ok_ || ltr553_ == nullptr) {
        return;
    }
    bool saturated = false;
    int ps = ltr553_->ReadPsRaw(&saturated);
    if (ps < 0) {
        return;  // transient I2C failure; keep previous state
    }
    last_ps_raw_ = ps;

    // Periodic raw dump every 2 s, DEBUG level (calibration 2026-06-13:
    // baseline ~380, hand at <=10cm ~820-1280).
    if (++prox_debug_tick_count_ >= PROX_DEBUG_LOG_TICKS) {
        prox_debug_tick_count_ = 0;
        ESP_LOGD(TAG, "proximity debug: ps_raw=%d sat=%d (threshold=%d)",
                 ps, saturated ? 1 : 0, prox_ps_threshold_);
    }

    if (ps >= prox_ps_threshold_) {
        if (prox_over_count_ < PROX_DEBOUNCE_SAMPLES) {
            prox_over_count_++;
        }
    } else {
        prox_over_count_ = 0;
    }
    bool detected = prox_over_count_ >= PROX_DEBOUNCE_SAMPLES;
    if (detected != prox_detected_prev_) {
        // Calibration aid: one INFO line per state change with the raw
        // value that caused it.
        ESP_LOGI(TAG, "proximity state: %s ps_raw=%d sat=%d (threshold=%d)",
                 detected ? "NEAR" : "FAR", ps, saturated ? 1 : 0,
                 prox_ps_threshold_);
    }
    if (prox_mode_ != ProxMode::Off && detected && !prox_detected_prev_) {
        // While a listen is open, a 2nd wave must always be able to stop
        // it (send the recording), so bypass the cooldown in that case.
        // The cooldown still gates back-to-back *starts* (anti-bounce).
        bool listening_now =
            prox_mode_ == ProxMode::Listen &&
            Application::GetInstance().GetDeviceState() == kDeviceStateListening;
        uint64_t now_us = esp_timer_get_time();
        if (listening_now || now_us >= prox_cooldown_until_us_) {
            HandleProximity(ps);
            prox_cooldown_until_us_ =
                now_us + (uint64_t)PROX_COOLDOWN_MS * 1000ULL;
        } else {
            ESP_LOGI(TAG, "proximity reaction suppressed (cooldown, %d ms left)",
                     (int)((prox_cooldown_until_us_ - now_us) / 1000ULL));
        }
    }
    prox_detected_prev_ = detected;

}

void StackChanBoard::InitializeLtr553Proximity() {

    ESP_LOGI(TAG, "Init LTR-553 proximity sensor (I2C addr 0x%02X)",
             Ltr553::DEFAULT_ADDR);
    ltr553_ = std::unique_ptr<Ltr553>(new Ltr553(i2c_bus_));
    ltr553_ok_ = ltr553_->Begin();
    if (!ltr553_ok_) {
        ESP_LOGW(TAG, "LTR-553 not detected; proximity reflex disabled (other features unaffected)");
        ltr553_.reset();
        return;
    }

    {
        Settings settings("stackchan_prox");
        std::string mode_str = settings.GetString("mode", "");
        if (!StringToProxMode(mode_str, &prox_mode_)) {
            // No (or invalid) "mode" key: migrate from the legacy
            // "enabled" bool written before mode was introduced.
            // enabled=true -> the new default (listen), false -> off.
            // The legacy key is left in place (harmless, read-only).
            bool legacy_enabled = settings.GetBool("enabled", true);
            prox_mode_ = legacy_enabled ? PROX_MODE_DEFAULT : ProxMode::Off;
        }
        prox_ps_threshold_ =
            settings.GetInt("threshold", PROX_PS_THRESHOLD_DEFAULT);
    }
    ESP_LOGI(TAG, "proximity config: mode=%s threshold=%d",
             ProxModeToString(prox_mode_), prox_ps_threshold_);

    esp_timer_create_args_t poll_args = {
        .callback = &StackChanBoard::ProximityPollCb,
        .arg = this,
        .dispatch_method = ESP_TIMER_TASK,
        .name = "prox_poll",
        .skip_unhandled_events = true,
    };
    ESP_ERROR_CHECK(esp_timer_create(&poll_args, &prox_poll_timer_));
    ESP_ERROR_CHECK(esp_timer_start_periodic(prox_poll_timer_,
                                             (uint64_t)PROX_POLL_MS * 1000));
    ESP_LOGI(TAG, "LTR-553 proximity poll started (%d ms interval)", PROX_POLL_MS);

}
