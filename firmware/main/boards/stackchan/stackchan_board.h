#ifndef STACKCHAN_BOARD_H
#define STACKCHAN_BOARD_H

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

#define TAG "StackChanBoard"

class Pmic : public Axp2101 {
public:
    // Power Init
    Pmic(i2c_master_bus_handle_t i2c_bus, uint8_t addr) : Axp2101(i2c_bus, addr) {
        uint8_t data = ReadReg(0x90);
        data |= 0b10110100;
        WriteReg(0x90, data);
        WriteReg(0x99, (0b11110 - 5));
        WriteReg(0x97, (0b11110 - 2));
        WriteReg(0x69, 0b00110101);
        WriteReg(0x30, 0b111111);
        WriteReg(0x90, 0xBF);
        WriteReg(0x94, 33 - 5);
        WriteReg(0x95, 33 - 5);
    }

    void SetBrightness(uint8_t brightness) {
        brightness = ((brightness + 641) >> 5);
        WriteReg(0x99, brightness);
    }
};

class CustomBacklight : public Backlight {
public:
    CustomBacklight(Pmic *pmic) : pmic_(pmic) {}

    void SetBrightnessImpl(uint8_t brightness) override {
        pmic_->SetBrightness(target_brightness_);
        brightness_ = target_brightness_;
    }

private:
    Pmic *pmic_;
};

class Aw9523 : public I2cDevice {
public:
    // Exanpd IO Init
    Aw9523(i2c_master_bus_handle_t i2c_bus, uint8_t addr) : I2cDevice(i2c_bus, addr) {
        WriteReg(0x02, 0b00000111);  // P0
        WriteReg(0x03, 0b10001111);  // P1
        WriteReg(0x04, 0b00011000);  // CONFIG_P0
        WriteReg(0x05, 0b00001100);  // CONFIG_P1
        WriteReg(0x11, 0b00010000);  // GCR P0 port is Push-Pull mode.
        WriteReg(0x12, 0b11111111);  // LEDMODE_P0
        WriteReg(0x13, 0b11111111);  // LEDMODE_P1
    }

    void ResetAw88298() {
        ESP_LOGI(TAG, "Reset AW88298");
        WriteReg(0x02, 0b00000011);
        vTaskDelay(pdMS_TO_TICKS(10));
        WriteReg(0x02, 0b00000111);
        vTaskDelay(pdMS_TO_TICKS(50));
    }

    void ResetIli9342() {
        ESP_LOGI(TAG, "Reset IlI9342");
        WriteReg(0x03, 0b10000001);
        vTaskDelay(pdMS_TO_TICKS(20));
        WriteReg(0x03, 0b10000011);
        vTaskDelay(pdMS_TO_TICKS(10));
    }
};

class Ft6336 : public I2cDevice {
public:
    struct TouchPoint_t {
        int num = 0;
        int x = -1;
        int y = -1;
    };
    
    Ft6336(i2c_master_bus_handle_t i2c_bus, uint8_t addr) : I2cDevice(i2c_bus, addr) {
        uint8_t chip_id = ReadReg(0xA3);
        ESP_LOGI(TAG, "Get chip ID: 0x%02X", chip_id);
        read_buffer_ = new uint8_t[6];
    }

    ~Ft6336() {
        delete[] read_buffer_;
    }

    void UpdateTouchPoint() {
        ReadRegs(0x02, read_buffer_, 6);
        tp_.num = read_buffer_[0] & 0x0F;
        tp_.x = ((read_buffer_[1] & 0x0F) << 8) | read_buffer_[2];
        tp_.y = ((read_buffer_[3] & 0x0F) << 8) | read_buffer_[4];
    }

    inline const TouchPoint_t& GetTouchPoint() {
        return tp_;
    }

private:
    uint8_t* read_buffer_ = nullptr;
    TouchPoint_t tp_;
};

// Minimal PY32 IO Expander driver (servo power switch on pin 0 / VM EN).
// Ported from M5Stack-BSP PY32IOExpander.cpp.
//
// IMPORTANT: this class deliberately does NOT inherit from I2cDevice.
// The base I2cDevice registers every device at scl_speed_hz = 400 kHz, but
// the M5 reference implementation (`PY32IOExpander_Class`) defaults to
// 100 kHz, and 400 kHz appears to leave PY32 in a half-finished slave
// state — `i2c_master_probe` returns ACK but the very next
// `i2c_master_transmit_receive` for REG_VERSION times out (0x103) every
// time. We register our own i2c_master device handle at 100 kHz to match
// the M5 default. Other peripherals on the bus (Si12T at 0x68, AXP2101,
// AW9523, FT6336) keep using the 400 kHz path through I2cDevice.
//
// Reliability notes:
//  - Each I2C op transparently retries up to I2C_INNER_RETRIES on transient
//    errors, with a short vTaskDelay between attempts.
//  - All bit-level write helpers propagate success as bool so the caller
//    can decide whether the GPIO actually got configured.
//  - Begin() can optionally return the version byte so the caller can log
//    which attempt finally talked to the chip.
class Py32IoExpander {
public:
    static constexpr uint8_t  DEFAULT_ADDR = 0x6F;
    static constexpr uint32_t I2C_FREQ_HZ  = 100000;  // 100 kHz (M5 default)
    static constexpr uint8_t  REG_GPIO_O_L_PUBLIC = 0x05;  // exposed for verify

    Py32IoExpander(i2c_master_bus_handle_t i2c_bus, uint8_t addr = DEFAULT_ADDR) {
        i2c_device_config_t cfg = {
            .dev_addr_length = I2C_ADDR_BIT_LEN_7,
            .device_address  = addr,
            .scl_speed_hz    = I2C_FREQ_HZ,
            .scl_wait_us     = 0,
            .flags           = { .disable_ack_check = 0 },
        };
        ESP_ERROR_CHECK(i2c_master_bus_add_device(i2c_bus, &cfg, &i2c_device_));
    }

    // Probe the chip. On success, returns true and (if non-null) writes the
    // version byte to out_version. Internal reads use SafeReadReg, which
    // already retries on transient I2C errors — so the chip is genuinely
    // unreachable / not yet ready when this returns false.
    bool Begin(uint8_t* out_version = nullptr) {
        uint8_t version = 0;
        if (!SafeReadReg(REG_VERSION, &version)) {
            return false;
        }
        if (version == 0x00 || version == 0xFF) {
            return false;
        }
        if (out_version != nullptr) {
            *out_version = version;
        }
        return true;
    }

    // direction: false=input, true=output. Accepts pin 0..15 (PY32 has 14
    // GPIOs; the WS2812 data line is on pin 13, in the high byte).
    bool SetDirection(uint8_t pin, bool output) {
        return WriteBitWideSafe(REG_GPIO_M_L, REG_GPIO_M_H, pin, output);
    }

    // mode: false=pull down, true=pull up. Accepts pin 0..15.
    bool SetPullMode(uint8_t pin, bool up) {
        if (up) {
            bool a = WriteBitWideSafe(REG_GPIO_PD_L, REG_GPIO_PD_H, pin, false);
            bool b = WriteBitWideSafe(REG_GPIO_PU_L, REG_GPIO_PU_H, pin, true);
            return a && b;
        } else {
            bool a = WriteBitWideSafe(REG_GPIO_PU_L, REG_GPIO_PU_H, pin, false);
            bool b = WriteBitWideSafe(REG_GPIO_PD_L, REG_GPIO_PD_H, pin, true);
            return a && b;
        }
    }

    bool DigitalWrite(uint8_t pin, bool level) {
        return WriteBitSafe(REG_GPIO_O_L, pin, level);
    }

    // Read back the current output low-byte register (pins 0..7) for
    // verification after DigitalWrite. Returns false if the read failed.
    bool ReadOutputLow(uint8_t* out) {
        return SafeReadReg(REG_GPIO_O_L, out);
    }

    // Drive mode for any pin (0..15). false=push-pull, true=open-drain.
    // The WS2812 data line on pin 13 must be push-pull.
    bool SetDriveMode(uint8_t pin, bool open_drain) {
        return WriteBitWideSafe(REG_GPIO_DRV_L, REG_GPIO_DRV_H, pin, open_drain);
    }

    // ---- LED (WS2812 driven by the PY32 itself, data line on pin 13) ----
    // REG_LED_CFG packs both the LED count (bits 0-5, max 32) and the latch
    // trigger (bit 6). Writing the count clears bit 6, which is fine because
    // it's a self-clearing strobe. RefreshLeds() does read-modify-write so
    // the count is preserved when we latch.
    bool SetLedCount(uint8_t count) {
        if (count > 32) count = 32;
        return SafeWriteReg(REG_LED_CFG, count & 0x3F);
    }

    // Set one LED to RGB888. RGB888 → RGB565 packing matches the M5 BSP:
    // ((r&0xF8)<<8) | ((g&0xFC)<<3) | (b>>3), little-endian on the wire.
    // Does NOT latch — call RefreshLeds() once after a batch of updates.
    bool SetLedColor(uint8_t index, uint8_t r, uint8_t g, uint8_t b) {
        if (index >= 32) return false;
        uint16_t v = (uint16_t)(((r & 0xF8) << 8) | ((g & 0xFC) << 3) | (b >> 3));
        uint8_t buf[3] = { (uint8_t)(REG_LED_RAM_START + index * 2),
                           (uint8_t)(v & 0xFF),
                           (uint8_t)((v >> 8) & 0xFF) };
        return SafeWriteRaw(buf, sizeof(buf));
    }

    // Burst-write up to N LED RGB565 pairs starting at index 0. data is
    // packed { lo0, hi0, lo1, hi1, ... } and len is the byte count
    // (=2*num_leds, max 64). Single I2C transaction — much faster than
    // calling SetLedColor in a loop. Does NOT latch.
    bool SetLedData(const uint8_t* data, size_t len) {
        if (data == nullptr || len == 0) return false;
        if (len > 64) len = 64;
        uint8_t buf[1 + 64];
        buf[0] = REG_LED_RAM_START;
        for (size_t i = 0; i < len; i++) buf[1 + i] = data[i];
        return SafeWriteRaw(buf, 1 + len);
    }

    // Latch the LED RAM out to the WS2812 strip. Read-modify-write so the
    // current count (bits 0-5) is preserved alongside the latch bit (bit 6).
    bool RefreshLeds() {
        uint8_t cfg = 0;
        if (!SafeReadReg(REG_LED_CFG, &cfg)) return false;
        return SafeWriteReg(REG_LED_CFG, (uint8_t)(cfg | (1u << 6)));
    }

private:
    // Owned device handle (NOT inherited from I2cDevice — see class comment).
    // Registered at 100 kHz in the constructor so this transport runs slower
    // than the rest of the bus.
    i2c_master_dev_handle_t i2c_device_ = nullptr;

    static constexpr uint8_t REG_VERSION  = 0x02;
    static constexpr uint8_t REG_GPIO_M_L = 0x03;  // Direction (mode) low byte
    static constexpr uint8_t REG_GPIO_M_H = 0x04;  // Direction (mode) high byte
    static constexpr uint8_t REG_GPIO_O_L = 0x05;  // Output low byte
    static constexpr uint8_t REG_GPIO_O_H = 0x06;  // Output high byte
    static constexpr uint8_t REG_GPIO_PU_L = 0x09; // Pull-up low byte
    static constexpr uint8_t REG_GPIO_PU_H = 0x0A; // Pull-up high byte
    static constexpr uint8_t REG_GPIO_PD_L = 0x0B; // Pull-down low byte
    static constexpr uint8_t REG_GPIO_PD_H = 0x0C; // Pull-down high byte
    static constexpr uint8_t REG_GPIO_DRV_L = 0x13; // Drive mode low byte
    static constexpr uint8_t REG_GPIO_DRV_H = 0x14; // Drive mode high byte
    static constexpr uint8_t REG_LED_CFG       = 0x24;  // count[5:0] + latch[6]
    static constexpr uint8_t REG_LED_RAM_START = 0x30;  // 2 bytes per LED, RGB565 LE

    // I2C op transient-retry parameters. Total budget per failed op is
    // (I2C_INNER_RETRIES - 1) * I2C_RETRY_DELAY_MS, i.e. ~30 ms here.
    static constexpr int I2C_INNER_RETRIES  = 3;
    static constexpr int I2C_RETRY_DELAY_MS = 15;

    // Safe I2C read — retries up to I2C_INNER_RETRIES on transient errors,
    // logs WARN only on the final failure to keep the log readable.
    bool SafeReadReg(uint8_t reg, uint8_t* out) {
        esp_err_t err = ESP_FAIL;
        for (int i = 0; i < I2C_INNER_RETRIES; i++) {
            err = i2c_master_transmit_receive(i2c_device_, &reg, 1, out, 1, 100);
            if (err == ESP_OK) {
                return true;
            }
            if (i + 1 < I2C_INNER_RETRIES) {
                vTaskDelay(pdMS_TO_TICKS(I2C_RETRY_DELAY_MS));
            }
        }
        ESP_LOGW("Py32IoExpander", "I2C read reg 0x%02X failed after %d tries: 0x%X",
                 reg, I2C_INNER_RETRIES, err);
        return false;
    }

    // Safe I2C write — same retry semantics as SafeReadReg.
    bool SafeWriteReg(uint8_t reg, uint8_t value) {
        uint8_t buffer[2] = {reg, value};
        esp_err_t err = ESP_FAIL;
        for (int i = 0; i < I2C_INNER_RETRIES; i++) {
            err = i2c_master_transmit(i2c_device_, buffer, 2, 100);
            if (err == ESP_OK) {
                return true;
            }
            if (i + 1 < I2C_INNER_RETRIES) {
                vTaskDelay(pdMS_TO_TICKS(I2C_RETRY_DELAY_MS));
            }
        }
        ESP_LOGW("Py32IoExpander", "I2C write reg 0x%02X failed after %d tries: 0x%X",
                 reg, I2C_INNER_RETRIES, err);
        return false;
    }

    // Read-Modify-Write a single bit using safe I2C. Pin 0..7 only.
    // Returns false if either the read or the write step ultimately failed.
    bool WriteBitSafe(uint8_t reg, uint8_t pin, bool value) {
        if (pin >= 8) {
            return false;
        }
        uint8_t v = 0;
        if (!SafeReadReg(reg, &v)) {
            return false;
        }
        if (value) {
            v |= (uint8_t)(1u << pin);
        } else {
            v &= (uint8_t)~(1u << pin);
        }
        return SafeWriteReg(reg, v);
    }

    // 16-bit RMW: pin 0..7 -> reg_l, pin 8..15 -> reg_h. Used for any pin
    // beyond pin 7 (LED data line is on pin 13, so all the LED setup goes
    // through this path).
    bool WriteBitWideSafe(uint8_t reg_l, uint8_t reg_h, uint8_t pin, bool value) {
        if (pin >= 16) return false;
        uint8_t reg = (pin < 8) ? reg_l : reg_h;
        uint8_t bit = (uint8_t)(pin & 0x07);
        uint8_t v = 0;
        if (!SafeReadReg(reg, &v)) return false;
        if (value) v |= (uint8_t)(1u << bit);
        else       v &= (uint8_t)~(1u << bit);
        return SafeWriteReg(reg, v);
    }

    // Burst write: ship a pre-built {reg, ...payload} buffer in a single
    // i2c_master_transmit. Used by the LED RAM writes which would otherwise
    // require dozens of individual register writes. Same retry semantics
    // as SafeWriteReg.
    bool SafeWriteRaw(const uint8_t* buf, size_t len) {
        esp_err_t err = ESP_FAIL;
        for (int i = 0; i < I2C_INNER_RETRIES; i++) {
            err = i2c_master_transmit(i2c_device_, buf, len, 100);
            if (err == ESP_OK) {
                return true;
            }
            if (i + 1 < I2C_INNER_RETRIES) {
                vTaskDelay(pdMS_TO_TICKS(I2C_RETRY_DELAY_MS));
            }
        }
        ESP_LOGW("Py32IoExpander", "I2C raw write (len=%u) failed after %d tries: 0x%X",
                 (unsigned)len, I2C_INNER_RETRIES, err);
        return false;
    }
};

// Minimal Si12T driver (12-channel capacitive touch sensor, TSM12-compatible).
// Used for the StackChan head-stroke / head-tap detection. Only the read path
// (Output1 register, channels 1-4) is needed for Phase 7. We expose just the
// first three channels through ReadTouchState() because the StackChan head
// has 3 conductive zones wired to TS1..TS3.
//
// Datasheet excerpt:
//   - I2C 7-bit address: 0xD0 >> 1 == 0x68 when ID_SEL pin is tied to GND.
//     This matches the address probed at boot ("0x68").
//   - Reset value of CTRL (0x09) is 0b00000111 (SLEEP=1). We must clear SLEEP
//     to enter normal sensing mode: CTRL = 0b00000011.
//   - Output1 (0x10) packs four channels into one byte (2 bits per channel):
//       bit[1:0] = OUT1, bit[3:2] = OUT2, bit[5:4] = OUT3, bit[7:6] = OUT4
//       00 = no output, 01 = low, 10 = medium, 11 = high.
//   - There is no dedicated chip-id register, so Begin() validates the device
//     via successful I2C ACK on the CTRL read + non-0xFF Output1 read.
class Si12T : public I2cDevice {
public:
    static constexpr uint8_t DEFAULT_ADDR = 0x68;  // ID_SEL = GND

    struct TouchState {
        bool zone[3];          // CH1, CH2, CH3 — true if any output level set
        uint8_t output1_raw;   // raw Output1 register byte (0x10)
        bool ok;               // false if the I2C read failed
    };

    Si12T(i2c_master_bus_handle_t i2c_bus, uint8_t addr = DEFAULT_ADDR)
        : I2cDevice(i2c_bus, addr) {}

    // Probe the chip and bring it out of sleep. Returns true on success.
    bool Begin() {
        uint8_t ctrl = 0;
        if (!SafeReadReg(REG_CTRL, &ctrl)) {
            return false;
        }
        // CTRL bit1 = SLEEP. Clear it; bit1:0 must hold 1 per datasheet
        // ("CTRL Bit1, Bit0 = 1 1" reset value), so write 0b00000011.
        if (!SafeWriteReg(REG_CTRL, 0x03)) {
            return false;
        }
        // Verify the device actually responds on the output register.
        // 0xFF would indicate an open bus / no device.
        uint8_t out1 = 0;
        if (!SafeReadReg(REG_OUTPUT1, &out1)) {
            return false;
        }
        if (out1 == 0xFF) {
            ESP_LOGW("Si12T", "Output1 read 0xFF (likely no device)");
            return false;
        }
        ESP_LOGI("Si12T", "init OK: ctrl=0x%02X out1=0x%02X (sleep cleared)", ctrl, out1);
        return true;
    }

    // Sample channels CH1..CH3 from Output1 (0x10). Single-shot read; the
    // caller is expected to debounce / interpret duration externally.
    TouchState ReadTouchState() {
        TouchState s = {};
        s.ok = false;
        if (!SafeReadReg(REG_OUTPUT1, &s.output1_raw)) {
            return s;
        }
        s.ok = true;
        // Each channel uses 2 bits; nonzero = touched at some level.
        s.zone[0] = ((s.output1_raw >> 0) & 0x3) != 0;  // CH1
        s.zone[1] = ((s.output1_raw >> 2) & 0x3) != 0;  // CH2
        s.zone[2] = ((s.output1_raw >> 4) & 0x3) != 0;  // CH3
        return s;
    }

private:
    static constexpr uint8_t REG_CTRL    = 0x09;  // CTRL, SLEEP bit etc.
    static constexpr uint8_t REG_OUTPUT1 = 0x10;  // CH1..CH4 packed (2bpp)

    bool SafeReadReg(uint8_t reg, uint8_t* out) {
        esp_err_t err = i2c_master_transmit_receive(i2c_device_, &reg, 1, out, 1, 100);
        if (err != ESP_OK) {
            ESP_LOGW("Si12T", "I2C read reg 0x%02X failed: 0x%X", reg, err);
            return false;
        }
        return true;
    }

    bool SafeWriteReg(uint8_t reg, uint8_t value) {
        uint8_t buffer[2] = {reg, value};
        esp_err_t err = i2c_master_transmit(i2c_device_, buffer, 2, 100);
        if (err != ESP_OK) {
            ESP_LOGW("Si12T", "I2C write reg 0x%02X failed: 0x%X", reg, err);
            return false;
        }
        return true;
    }
};

// Minimal LTR-553ALS-WA driver (Lite-On ambient light + proximity sensor,
// built into the CoreS3 front panel). Phase C1 only needs the proximity (PS)
// channel, so the ALS side is left in its power-up standby state.
//
// Datasheet excerpt (Lite-On BNS-OD-C131/A4 Rev 1.0, 22 May 2013):
//   - I2C 7-bit address: 0x23 (write 0x46 / read 0x47).
//   - PART_ID (0x86, RO) = 0x92, MANUFAC_ID (0x87, RO) = 0x05. Used by
//     Begin() to validate the device before any configuration write.
//   - PS_CONTR (0x81): bits[1:0] = PS mode, 00 = standby (power-up default),
//     10 / 11 = active. Bits[3:2] = PS gain (00 = x16 default, 10 = x32,
//     11 = x64). Bit[5] = PS Saturation Indicator Enable; PS_DATA_1[7]
//     always reads 0 unless this bit is set.
//   - PS_LED (0x82, default 0x7F): bits[7:5] LED pulse freq (011 = 60 kHz),
//     bits[4:3] duty (11 = 100%), bits[2:0] peak current (111 = 100 mA... the
//     datasheet table tops out at 100 mA for codes 100..111).
//   - PS_N_PULSES (0x83, default 0x01): LED pulse count per measurement,
//     bits[3:0] = 0001..1111 (1..15 pulses).
//   - PS_MEAS_RATE (0x84, default 0x02 = 100 ms): PS_DATA update interval
//     in active mode. We program 0x00 (50 ms) so the 100 ms reflex poll
//     never reads the same stale measurement twice.
//   - PS_DATA_0 (0x8D) / PS_DATA_1 (0x8E): 11-bit PS value, low byte in
//     PS_DATA_0, upper 3 bits in PS_DATA_1[2:0]; PS_DATA_1[7] is the
//     saturation flag. Both registers are locked for the duration of one
//     I2C read operation, so a single 2-byte burst read starting at 0x8D
//     yields a coherent sample.
//   - Timing: initial start-up 100 ms (max), standby-to-active wake-up
//     10 ms (max). Both are covered by the boot sequence + the first
//     poll tick landing >= PROX_POLL_MS after Begin().
class Ltr553 : public I2cDevice {
public:
    static constexpr uint8_t DEFAULT_ADDR = 0x23;

    Ltr553(i2c_master_bus_handle_t i2c_bus, uint8_t addr = DEFAULT_ADDR)
        : I2cDevice(i2c_bus, addr) {}

    // Probe the chip via PART_ID / MANUFAC_ID, then configure and activate
    // the PS channel. Returns true on success.
    bool Begin() {
        uint8_t part_id = 0;
        if (!SafeReadReg(REG_PART_ID, &part_id) || part_id != EXPECTED_PART_ID) {
            ESP_LOGW("Ltr553", "PART_ID mismatch: got 0x%02X want 0x%02X",
                     part_id, EXPECTED_PART_ID);
            return false;
        }
        uint8_t manufac_id = 0;
        if (!SafeReadReg(REG_MANUFAC_ID, &manufac_id) ||
            manufac_id != EXPECTED_MANUFAC_ID) {
            ESP_LOGW("Ltr553", "MANUFAC_ID mismatch: got 0x%02X want 0x%02X",
                     manufac_id, EXPECTED_MANUFAC_ID);
            return false;
        }
        // Maximum-sensitivity PS setup: the CoreS3 front panel sits between
        // the sensor and the target, so the IR round-trip is heavily
        // attenuated (real-device Phase C1 bring-up read ps_raw=0 even with
        // a hand at the sensor window using the power-up defaults).
        // LED drive: 60 kHz / 100% duty / 100 mA peak — already the table
        // maximum, written explicitly for visibility.
        if (!SafeWriteReg(REG_PS_LED, 0x7F)) return false;
        // 15 LED pulses per measurement (table maximum; default is 1).
        // More pulses integrate more reflected IR energy per sample.
        if (!SafeWriteReg(REG_PS_N_PULSES, 0x0F)) return false;
        // PS measurement repeat rate 50 ms (faster than the 100 ms poll).
        if (!SafeWriteReg(REG_PS_MEAS_RATE, 0x00)) return false;
        // PS active mode (bits1:0 = 11), gain x64 (bits3:2 = 11, table
        // maximum; default x16), saturation indicator enabled (bit5 = 1)
        // so PS_DATA_1[7] is meaningful — needed to tell "panel blocks
        // all IR" (ps_raw stays 0) apart from "IC saturates" (flag set).
        if (!SafeWriteReg(REG_PS_CONTR, 0x2F)) return false;
        ESP_LOGI("Ltr553", "init OK: part_id=0x%02X manufac_id=0x%02X "
                 "(PS active, gain x64, 15 pulses, 50 ms rate, sat indicator on)",
                 part_id, manufac_id);
        return true;
    }

    // Single-shot PS read. Returns the 11-bit raw value (0..2047) or -1 on
    // I2C failure. The 2-byte burst read keeps the sample coherent (the
    // chip locks PS_DATA_0/1 for the duration of one read operation).
    // If `saturated` is non-null it receives PS_DATA_1[7] (the saturation
    // flag; only meaningful with PS_CONTR bit5 set, which Begin() does).
    int ReadPsRaw(bool* saturated = nullptr) {
        uint8_t buf[2] = {0, 0};
        uint8_t reg = REG_PS_DATA_0;
        esp_err_t err = i2c_master_transmit_receive(i2c_device_, &reg, 1,
                                                    buf, 2, 100);
        if (err != ESP_OK) {
            ESP_LOGW("Ltr553", "I2C PS data read failed: 0x%X", err);
            return -1;
        }
        if (saturated != nullptr) {
            *saturated = (buf[1] & 0x80) != 0;
        }
        return ((buf[1] & 0x07) << 8) | buf[0];
    }

private:
    static constexpr uint8_t REG_PS_CONTR     = 0x81;
    static constexpr uint8_t REG_PS_LED       = 0x82;
    static constexpr uint8_t REG_PS_N_PULSES  = 0x83;
    static constexpr uint8_t REG_PS_MEAS_RATE = 0x84;
    static constexpr uint8_t REG_PART_ID      = 0x86;
    static constexpr uint8_t REG_MANUFAC_ID   = 0x87;
    static constexpr uint8_t REG_PS_DATA_0    = 0x8D;

    static constexpr uint8_t EXPECTED_PART_ID    = 0x92;
    static constexpr uint8_t EXPECTED_MANUFAC_ID = 0x05;

    bool SafeReadReg(uint8_t reg, uint8_t* out) {
        esp_err_t err = i2c_master_transmit_receive(i2c_device_, &reg, 1, out, 1, 100);
        if (err != ESP_OK) {
            ESP_LOGW("Ltr553", "I2C read reg 0x%02X failed: 0x%X", reg, err);
            return false;
        }
        return true;
    }

    bool SafeWriteReg(uint8_t reg, uint8_t value) {
        uint8_t buffer[2] = {reg, value};
        esp_err_t err = i2c_master_transmit(i2c_device_, buffer, 2, 100);
        if (err != ESP_OK) {
            ESP_LOGW("Ltr553", "I2C write reg 0x%02X failed: 0x%X", reg, err);
            return false;
        }
        return true;
    }
};

class StackChanBoard : public WifiBoard {
private:
    // Internal I2C bus (shared by AXP2101 / AW9523 / FT6336 / PY32 / Si12T /
    // audio codec / IMU). Direct on-board ICs only; not exposed through
    // self.i2c.* MCP tools.
    i2c_master_bus_handle_t i2c_bus_;
    // External I2C bus dedicated to Grove Port A. Exposed through self.i2c.*
    // MCP tools so the gateway can drive attached M5Stack Unit modules.
    i2c_master_bus_handle_t port_a_i2c_bus_;
    // Port B WS2812 generic strip state (driven from MCP tools self.port_b.ws2812.*).
    // Independent from the on-board PY32-driven 12-LED base strip (self.led.*),
    // which uses I2C -> PY32 internal WS2812 engine. The two paths share no
    // hardware peripheral and no software state; existing self.led.* behaviour
    // is byte-for-byte unchanged.
    bool ws2812_ok_ = false;
    uint16_t ws2812_led_count_ = 0;
    led_strip_handle_t ws2812_handle_ = nullptr;
    static constexpr gpio_num_t PORT_B_WS2812_DATA_PIN = GPIO_NUM_9;  // CoreS3 HY2.0-4P (Port B) digital OUTPUT
    static constexpr uint16_t PORT_B_WS2812_MAX_LEDS = 256;
    Pmic* pmic_;
    Aw9523* aw9523_;
    Ft6336* ft6336_;
    LcdDisplay* display_;
    EspVideo* camera_;
    esp_timer_handle_t touchpad_timer_;
    PowerSaveTimer* power_save_timer_;
    ScsBus scs_bus_;
    std::unique_ptr<Py32IoExpander> io_expander_;

    // Avatar overlay state. avatar_img_ is created lazily on the active LVGL
    // screen because the screen tree (container_, emoji_label_, ...) is built
    // by Application::Start() -> Display::SetupUI(), which runs after this
    // board's constructor completes. avatar_init_timer_ retries every 500 ms
    // until the screen is ready, then stops itself.
    lv_obj_t* avatar_img_ = nullptr;
    esp_timer_handle_t avatar_init_timer_ = nullptr;
    std::string current_avatar_face_ = "idle";

    // Small status text overlay (Phase F). A short label kept in front of the
    // avatar near the top-centre of the LCD, used to surface gateway-side
    // status ("きいてるよ", "考え中", "調べ中", ...) via the
    // self.display.set_status_text MCP tool. Created lazily on the active
    // screen like avatar_img_, with its own visibility so set_avatar("off")
    // does not affect it. Inherits the screen's text font (the common puhui
    // font packed into assets, which carries Japanese glyphs).
    lv_obj_t* status_label_ = nullptr;

    // Subtitle overlay (Phase F). A multi-line caption pinned to the bottom
    // of the LCD, used by the gateway to show what the persona is speaking
    // via the self.display.set_subtitle MCP tool. Same lazy-create / own-
    // visibility model as status_label_; wraps to 2-3 lines and shares the
    // translucent black backing for legibility over the avatar.
    lv_obj_t* subtitle_label_ = nullptr;

    // Route badge overlay (Phase F). A tiny indicator in the top-right
    // corner driven by the self.display.set_route_badge MCP tool (the
    // gateway sends "H" while a turn is being served by the Hermes agent).
    // Same lazy-create / own-visibility model as status_label_; placed in a
    // corner so it does not collide with status_label_ (top-centre).
    lv_obj_t* route_badge_ = nullptr;

    // Dynamic avatar set loaded via the load_avatar_set MCP tool. Stays
    // unloaded by default — the index-based image lookups then fall back
    // to the static const tables in avatar_images.h (placeholder or local
    // override). See docs/intent/stackchan_avatar_pipeline.md in the
    // SAIVerse repository.
    AvatarSet avatar_set_;

    // ---- Avatar rendering state (Phase 4.5-a) -----------------------------
    //
    // The avatar is represented as three independent axes — face, eyes,
    // mouth — each carrying a 0-indexed slot identical to AvatarSet's
    // GetFace / GetEyes / GetMouth layout. The on-screen image is then
    // derived from this state in a mode-aware way (RenderAvatarLocked):
    //
    //   - Layered mode (or AvatarSet not loaded): there is no compositor
    //     on the firmware side — avatar_img_ shows exactly one image at a
    //     time. active_layer_ selects which axis drives the current frame
    //     (face during rest / mouth during set_mouth / eyes during blink),
    //     matching the upstream Phase 2 behaviour where blink temporarily
    //     replaces the face image and is then restored.
    //   - Matrix mode: avatar_set_.GetMatrix(face, eyes, mouth) returns the
    //     pre-composed image for the current (face, eyes, mouth) triple.
    //     active_layer_ is ignored; every state change updates the
    //     composed frame.
    //
    // Indices remain valid across mode switches so a future load_avatar_set
    // call into a different mode does not lose the persona's current
    // expression. current_avatar_face_ is kept as the string form because
    // existing internal callers (touch reactions, SetAvatarOff resume,
    // mouth-sequence restore) still address the face by name.
    enum class ActiveLayer : uint8_t {
        FACE = 0,
        EYES = 1,
        MOUTH = 2,
    };
    int current_face_index_ = 0;   // 0..5  (idle / happy / thinking / sad / surprised / embarrassed)
    int current_eyes_index_ = 0;   // 0..2  (open / half / closed) — 0 is the resting state
    int current_mouth_index_ = 0;  // 0..4  (closed / half / open / e / u) — 0 is the resting state
    ActiveLayer active_layer_ = ActiveLayer::FACE;

    // Pending state captured while an avatar_set_fetch is in flight.
    // Calls to set_avatar / set_mouth_shape / set_blink during a fetch
    // are buffered here and replayed once the new AvatarSet is loaded,
    // so the UI does not flicker between the old and partially-loaded
    // new sets. avatar_fetch_in_progress_ is the entry guard: a
    // concurrent avatar_set_fetch is rejected with
    // avatar_set_loaded error="fetch_in_progress" rather than racing
    // the worker task that is already running.
    std::atomic<bool> avatar_fetch_in_progress_{false};
    SemaphoreHandle_t avatar_pending_lock_ = nullptr;
    struct PendingAvatarState {
        bool has_off = false;
        bool has_face = false;     std::string face_name;
        bool has_mouth = false;    std::string mouth_shape;
        bool has_blink = false;    bool blink_enabled = false;
    };
    PendingAvatarState avatar_pending_;

    // Phase 2: blinking + lip-sync overlay state.
    // Blink works as a four-step state machine driven by blink_step_timer_:
    //   FACE -> EYES_HALF -> EYES_CLOSED -> EYES_HALF -> FACE (restore last face)
    // Each step is BLINK_STEP_MS apart. While a blink is in progress, further
    // schedule events are dropped (we run to completion before re-arming).
    // blink_schedule_timer_ fires every 3-6s (re-armed each cycle) and triggers
    // a new blink only if blink_enabled_ and no other blink is in flight.
    enum class BlinkState : uint8_t {
        IDLE = 0,
        EYES_HALF_DOWN,
        EYES_CLOSED,
        EYES_HALF_UP,
    };
    static constexpr int BLINK_STEP_MS = 100;
    static constexpr int BLINK_MIN_GAP_MS = 3000;
    static constexpr int BLINK_MAX_GAP_MS = 6000;
    esp_timer_handle_t blink_schedule_timer_ = nullptr;
    esp_timer_handle_t blink_step_timer_ = nullptr;
    BlinkState blink_state_ = BlinkState::IDLE;
    bool blink_enabled_ = false;
    // Captures blink_enabled_ at the moment SetAvatarOff() runs, so that a
    // later set_avatar(<other face>) can restore the previous blink state.
    // Only meaningful while current_avatar_face_ == "off".
    bool blink_enabled_before_off_ = false;

    // Phase 4 audio (Issue #76): state-driven TTS lip-sync animation.
    // Driven by the gateway's tts.start / tts.stop notifications (see
    // Application::OnIncomingJson) via Board::OnTtsStart / OnTtsStop;
    // cycles the mouth through closed -> half -> open -> half on a fixed
    // TTS_LIPSYNC_STEP_MS cadence until stopped. Autonomous blink is paused
    // while active (same Phase 2 trade-off as the mouth-sequence task: a
    // blink ending would otherwise restore the full-face image and overwrite
    // the mouth overlay). The user's most recent blink intent is read from
    // blink_desired_ at stop so a set_blink call issued during playback is
    // honoured.
    enum class TtsLipSyncShape : uint8_t {
        CLOSED = 0,
        HALF_RISING,   // closed -> open transition
        OPEN,
        HALF_FALLING,  // open -> closed transition
    };
    static constexpr int TTS_LIPSYNC_STEP_MS = 150;
    esp_timer_handle_t tts_lipsync_timer_ = nullptr;
    std::atomic<bool> tts_lipsync_active_{false};
    TtsLipSyncShape tts_lipsync_shape_ = TtsLipSyncShape::CLOSED;

    // Phase 7: Si12T head-touch sensing.
    // Polling every TOUCH_POLL_MS samples Output1 (CH1..CH3 -> 3 head zones).
    // Edge detection on the OR of the three zones produces TAP / STROKE
    // gestures based on hold duration:
    //   duration <  TAP_MAX_MS (400 ms)  -> TAP    -> face=surprised
    //   duration >= STROKE_MIN_MS (600 ms) -> STROKE -> face=embarrassed + servo wobble
    //   400 <= duration < 600 ms         -> treated as TAP (greyzone)
    // Reactions auto-revert to "idle" after REACTION_HOLD_MS (3 s). A
    // post-reaction COOLDOWN_MS lock-out prevents one head-pat from firing
    // a chain of events.
    enum class TouchEvent : uint8_t {
        IDLE = 0,
        TAP,
        STROKE,
    };
    static constexpr int TOUCH_POLL_MS    = 100;  // 100 Hz polling
    static constexpr int TAP_MAX_MS       = 400;
    static constexpr int STROKE_MIN_MS    = 400;  // was 600; lowered because
                                                  // finger-glide between zones
                                                  // and Si12T auto-recalibration
                                                  // inject brief "all-false"
                                                  // gaps that cut a real stroke
                                                  // short of 600 ms.
    static constexpr int REACTION_HOLD_MS = 3000;
    static constexpr int COOLDOWN_MS      = 800;  // post-reaction noise gate
    // Idle auto-settle: after this long with no face / head / LED activity,
    // recenter the head, return to the idle face and turn the base LEDs off.
    // Distinct from REACTION_HOLD_MS (the short per-reaction revert) — this is
    // the long "nobody is interacting" backstop, re-armed by every activity.
    static constexpr int IDLE_SETTLE_MS   = 60000; // 60 s
    // If the head is already within this many degrees of neutral when the
    // idle backstop fires, skip the WriteHeadAngles so torque is not
    // re-engaged on an already-settled head.
    static constexpr int IDLE_SETTLE_DEADBAND_DEG = 3;
    // With 2-sample debounce this gives ~200 ms confirm latency, fast enough
    // to catch a quick "pon" (~200 ms press) while still rejecting single-
    // sample jitter. Was 200 ms polling -> 400 ms confirm, which silently
    // dropped most short taps.
    static constexpr int SERVO_WOBBLE_STEP_MS = 350;  // was 200; SCS0009 needs
                                                       // ~125 ms to physically
                                                       // travel ±20°, plus the
                                                       // ACK round-trip + IFG.
                                                       // Tighter steps caused
                                                       // bus hangs.
    static constexpr int SERVO_WOBBLE_AMPLITUDE_DEG = 20;

    std::unique_ptr<Si12T> si12t_;
    bool si12t_ok_ = false;
    esp_timer_handle_t touch_poll_timer_ = nullptr;
    esp_timer_handle_t touch_revert_timer_ = nullptr;
    esp_timer_handle_t idle_settle_timer_ = nullptr;  // long idle backstop

    // Touch detection state (single-thread access from the touch_poll_timer_
    // callback, which runs on the ESP_TIMER_TASK).
    bool touch_pressed_prev_ = false;          // last sample (debounced)
    bool touch_pressed_pending_ = false;       // candidate awaiting confirm
    int  touch_pending_count_ = 0;             // consecutive samples matching
    uint64_t touch_press_start_us_ = 0;        // when pressed_prev_ went true
    uint64_t cooldown_until_us_ = 0;           // ignore press until this ts

    // Last reported event for MCP get_touch_state.
    TouchEvent last_event_ = TouchEvent::IDLE;
    uint64_t   last_event_us_ = 0;
    bool       last_zone_snapshot_[3] = {false, false, false};
    uint8_t    last_output1_raw_ = 0;
    // Press-start snapshot. last_* fields above are overwritten every poll
    // tick, so by the time HandleTap / HandleStroke fires on the falling edge
    // they reflect the release state (zones=000 raw=0x00). press_start_*
    // captures the rising-edge state so the log can show what the sensor
    // actually saw when the touch began. Useful for distinguishing genuine
    // touches (CH1〜CH3 set) from false positives (e.g. CH4 noise, raw=0x00
    // with press judged via debounce, etc.).
    bool       press_start_zones_[3] = {false, false, false};
    uint8_t    press_start_output1_raw_ = 0;

    // Phase C1: LTR-553ALS-WA proximity hand-wave reflex.
    // The CoreS3 front panel carries an LTR-553 (IR-reflective proximity +
    // ambient light) on the internal I2C bus at 0x23. Polling every
    // PROX_POLL_MS reads the 11-bit PS value; PROX_DEBOUNCE_SAMPLES
    // consecutive over-threshold samples confirm a "hand near" rising edge,
    // which triggers a board-local reflex (look up front + happy face) —
    // no Hermes / gateway round-trip, per the design principle that
    // low-level reflexes stay on the firmware. A rising-edge-only trigger
    // plus PROX_COOLDOWN_MS keeps a hand held over the sensor (or repeated
    // waving) from firing a chain of reactions.
    // Reflex re-enabled (2026-06-13 retest): contrary to the 2026-06-11
    // finding, a hand IS visible through the front shell. Measured ps_raw:
    // baseline 368-388 (panel crosstalk), hand at 1-2cm ~820-1090, hand at
    // 10cm ~1035-1277 (strongest — at point-blank range the reflected spot
    // partly misses the offset photodiode), hand at 20-30cm only ~393-448.
    // So the hand-wave reflex works up to ~10-15cm; room-scale presence
    // (1-2m) still needs an external ToF unit on Grove Port A.
    // The mode and threshold are runtime-tunable via the
    // self.touch.set_proximity_config MCP tool and persist in NVS
    // (namespace "stackchan_prox"); the defaults below apply on first boot.
    // Mode selects what a confirmed hand-wave does:
    //   reflex = look up + happy face (board-local, no Hermes round-trip)
    //   listen = start a tap-equivalent listen (record -> STT -> Hermes)
    //   off    = no reaction
    enum class ProxMode { Off, Reflex, Listen };
    static constexpr ProxMode PROX_MODE_DEFAULT = ProxMode::Listen;
    static constexpr int PROX_PS_THRESHOLD_DEFAULT    = 600;
                                                       // raw PS counts (0..2047):
                                                       // above the 20-30cm
                                                       // bystander band (<=448),
                                                       // below the weakest
                                                       // hand-signal (~820)
    static constexpr int PROX_POLL_MS          = 100;  // same cadence as touch poll
    static constexpr int PROX_DEBOUNCE_SAMPLES = 3;    // ~300 ms confirm; rejects
                                                       // single-sample IR glints
    static constexpr int PROX_COOLDOWN_MS      = 5000; // min gap between reflexes
    static constexpr int PROX_REACT_YAW_DEG    = 0;    // face front...
    static constexpr int PROX_REACT_PITCH_DEG  = 60;   // ...and slightly up
                                                       // (rest pose is 45)
    // ps_raw DEBUG-level dump cadence (20 x 100 ms = 2 s); enable with
    // esp_log_level_set when re-calibrating.
    static constexpr int PROX_DEBUG_LOG_TICKS  = 20;

    std::unique_ptr<Ltr553> ltr553_;
    bool ltr553_ok_ = false;
    esp_timer_handle_t prox_poll_timer_ = nullptr;

    // Runtime-tunable proximity config: loaded from NVS in
    // InitializeLtr553Proximity(), written by set_proximity_config (MCP
    // task). Read from the poll callback with the same benign torn-read
    // trade-off as last_ps_raw_ below.
    ProxMode prox_mode_         = PROX_MODE_DEFAULT;
    int      prox_ps_threshold_ = PROX_PS_THRESHOLD_DEFAULT;

    // Proximity detection state (single-thread access from the
    // prox_poll_timer_ callback on ESP_TIMER_TASK, same model as the
    // Si12T touch state above).
    int      prox_over_count_ = 0;          // consecutive over-threshold polls
    bool     prox_detected_prev_ = false;   // last debounced state
    uint64_t prox_cooldown_until_us_ = 0;   // suppress reflex until this ts
    int      prox_debug_tick_count_ = 0;    // TEMP DEBUG: 2 s log cadence
    // Latest raw PS sample for MCP visibility (read by get_touch_state on
    // the MCP task; benign torn-read trade-off identical to
    // last_output1_raw_).
    int      last_ps_raw_ = -1;             // -1 until the first good sample

    // Servo wobble sub-state. Keeps the previously-set angles untouched
    // before/after the wobble so that an external set_head_angles call is
    // not silently overwritten beyond the wobble window.
    std::atomic<int> servo_wobble_step_{0};       // 0..3 sequence index
    std::atomic<bool> servo_wobble_active_{false};

    // Shared motion state. This stays on the board singleton because boot-init
    // ReadPos restore / re-sync phases seed the same state before and after the
    // concrete MotionDriver is selected. Drivers only borrow these references.
    struct AxisMotion {
        int target_deg = 0;
        int start_deg = 0;
        int current_deg = 0;
        uint32_t move_start_ms = 0;
        // For the delegated driver: time the WritePos for this request
        // was successfully ACK'd by the servo (i.e. when the physical
        // motion actually began on the SCS0009's internal clock). May
        // be later than move_start_ms when the dispatch is delayed by
        // Tick wake or retry rounds. ApplyReadMoveResult's stuck-high
        // timeout is measured from dispatch_start_ms, not from staging,
        // so that degraded-bus latency does not cause premature force-
        // clear while the servo is genuinely still mid-motion. 0 means
        // "not yet dispatched"; ApplyReadMoveResult skips the
        // stuck-high check until dispatch_start_ms is populated by
        // FinishDispatch's write_ok branch. HostInterpolation path
        // does not consult this field.
        uint32_t dispatch_start_ms = 0;
        uint32_t move_duration_ms = 0;
        bool moving = false;
        // Monotonic counter incremented by ServoDelegatedMotionDriver::
        // StartMove on each dispatch stage. Used by Tick() to detect
        // when a newer StartMove has raced in between the snapshot at
        // the top of Tick() and the post-WritePos / post-ReadMove
        // commit (motion_mutex_ is dropped while the bus operation
        // runs). esp_timer_get_time()/1000 has 1 ms resolution and is
        // not unique enough on its own — two StartMove calls within
        // the same millisecond would collide on move_start_ms. The
        // HostInterpolation path does not consult this field; the
        // monotonic counter is owned by the delegated driver.
        uint64_t request_token = 0;
        // Set by the delegated driver when ApplyReadMoveResult's
        // 5-consecutive-failure force-clear fires: WritePos ACK
        // confirmed the servo received the command, but ReadMove
        // polling never observed completion. current_deg holds the
        // last optimistic commit (the requested target), but the
        // physical head may be mid-trajectory or at the wrong angle.
        // The next StartMove on this axis treats position_unknown as
        // a "force re-dispatch" signal (no-op skip is suppressed),
        // so a same-target retry surfaces the failure rather than
        // hiding it behind a stale-but-equal current_deg. The
        // HostInterpolation path does not consult this field.
        bool position_unknown = false;
    };
    class MotionDriver;
    // TODO: motion_mutex_/scs_bus_mutex_/servo_task_handle_ have no destroy path; board is singleton via DECLARE_BOARD.
    AxisMotion yaw_motion_;
    AxisMotion pitch_motion_;
    SemaphoreHandle_t motion_mutex_ = nullptr;     // protects AxisMotion fields
    SemaphoreHandle_t scs_bus_mutex_ = nullptr;    // serializes UART access (WritePos/ReadPos)
    std::unique_ptr<MotionDriver> motion_driver_;
    TaskHandle_t servo_task_handle_ = nullptr;
    uint32_t last_motion_end_ms_ = 0;              // ServoTask-private
    bool last_motion_end_valid_ = false;           // ServoTask-private
    std::atomic<bool> idle_timer_reset_pending_{false};
    enum class TorqueState : uint8_t {
        kEngaged = 0,
        kPartial = 1,
        kReleased = 2,
        kReleasing = 3,
        // Published when InternalSetServoTorque cannot confirm bus success
        // for both axes. Forward-progress invariant: the next motion / manual
        // call issues a real bus frame instead of short-circuiting. Mirrors
        // the WritePos retries-exhausted -> position_unknown convention for
        // the torque domain.
        kUncertain = 4,
    };
    std::atomic<TorqueState> torque_state_{TorqueState::kEngaged};
    std::atomic<uint32_t> torque_release_epoch_{0};
    // Per-axis commanded torque state is protected by scs_bus_mutex_;
    // torque_state_ publishes the derived cross-task summary.
    bool yaw_torque_enabled_ = true;               // protected by scs_bus_mutex_
    bool pitch_torque_enabled_ = true;             // protected by scs_bus_mutex_
    std::atomic<bool> boot_init_done_{false};
#if CONFIG_STACKCHAN_AUTO_TORQUE_RELEASE_ENABLED
    std::atomic<bool> auto_release_enabled_{true};
#else
    std::atomic<bool> auto_release_enabled_{false};
#endif
    static constexpr uint32_t MOTION_TICK_MS = 20;
    static constexpr uint32_t MOTION_DEFAULT_DURATION_MS = 600;
    // Speed-based motion API (Issue #129).
    // MIN_SMOOTH_SPEED_DPS is the on-device measured smoothness floor
    // (5 step/tick "transition out", measured 2026-05-15). Speeds below
    // this look textured on SCS0009 at MOTION_TICK_MS=20 ms; the firmware
    // permits sub-floor speeds (logged with ESP_LOGW) so callers like the
    // gateway "low" preset (30 dps) can deliver deliberately slow motion.
    static constexpr int MIN_SMOOTH_SPEED_DPS = 72;
    // MAX_SPEED_DPS is the SCS0009 datasheet reliability test working speed
    // (60 deg / 0.25 s = 240 deg/s, validated for >50k cycles at 1/2 rated load).
    static constexpr int MAX_SPEED_DPS = 240;
    // DEFAULT_SPEED_DPS is used when the caller passes speed_dps <= 0.
    // Matches the gateway "mid" preset.
    static constexpr int DEFAULT_SPEED_DPS = 120;
    static constexpr uint32_t MOTION_PER_WRITE_TIME_MS = 30;
    static constexpr uint32_t MOTION_POLL_INTERVAL_MS = 50;
    static constexpr uint32_t AUTO_TORQUE_RELEASE_MIN_MS = 500;
    static constexpr uint32_t AUTO_TORQUE_RELEASE_MAX_MS = 600000;
    static constexpr int kMaxReengageRetries = 3;
    static constexpr int kMaxManualReengageRetries = 3;
#ifdef CONFIG_STACKCHAN_AUTO_TORQUE_RELEASE_MS
    static constexpr uint32_t AUTO_TORQUE_RELEASE_DEFAULT_MS =
        CONFIG_STACKCHAN_AUTO_TORQUE_RELEASE_MS;
#else
    static constexpr uint32_t AUTO_TORQUE_RELEASE_DEFAULT_MS = 5000;
#endif
    std::atomic<uint32_t> auto_release_timeout_ms_{
        AUTO_TORQUE_RELEASE_DEFAULT_MS};

    // Issue #80 / #98: pitch is guarded by two complementary tiers.
    //
    // Tier 1 — Hard clamp [SAFE_PITCH_MIN, SAFE_PITCH_MAX]:
    //   The absolute mechanical safety net. Its only job is to prevent
    //   physical damage to the servo / gear / chassis. Values are silently
    //   clamped to this range at every servo-write boundary and an
    //   ESP_LOGW is emitted when clamping occurs.
    //   - Lower bound 0°: the mechanical end-stop on the M5Stack CoreS3 +
    //     SCS0009 hardware sits very close to pitch=-1° (validated on a
    //     real unit, PR #81). Driving below 0° presses the servo gear into
    //     the physical stopper and produces an audible click.
    //   - Upper bound (SAFE_PITCH_MAX): chosen 1° inside the validated
    //     mechanical upper limit. The M5Stack-documented servo features
    //     advertise "90-degree movement on the vertical axis", so the
    //     mechanical upper end-stop is expected near 90°; the precise
    //     value is established by real-device sweep (Issue #98 validation).
    //
    // Tier 2 — Recommended operating range [RECOMMENDED_PITCH_MIN,
    //          RECOMMENDED_PITCH_MAX]:
    //   The M5Stack-documented sweet spot for long-term servo reliability
    //   (https://docs.m5stack.com/en/StackChan, "Motion Angle Notice":
    //   "The movement angle of the StackChan Y-axis servo (vertical
    //   direction) is recommended to be controlled within 5 ~ 85°.
    //   Operating at extreme angles may cause servo stall and permanent
    //   damage.").
    //   Values inside the hard clamp but outside this range are accepted
    //   (they are not hardware-damaging on a single call), and an
    //   ESP_LOGI is emitted so callers / agents can notice the deviation
    //   without blocking the motion.
    //
    // Defense-in-depth: the hard clamp is enforced at every servo-write
    // boundary —
    //   1. PitchDegToPos() clamps its input (covers motion-task
    //      interpolation and any future caller that bypasses the MCP
    //      layer).
    //   2. The start-up restore from ReadPos clamps the recovered angle
    //      so a device booting with the head physically pushed past the
    //      safe range does not carry that out-of-range starting angle
    //      into motion interpolation.
    //   3. The set_head_angles MCP handler additionally clamps the
    //      request target so the original out-of-range value is logged.
    static constexpr int SAFE_PITCH_MIN = 0;
    static constexpr int SAFE_PITCH_MAX = 88;  // Issue #98: validated on real hardware
                                                // (M5Stack CoreS3 + SCS0009 ×2). On-device
                                                // sweep observed clean motion at pitch=85
                                                // and pitch=88 reached without end-stop, but
                                                // pitch=89 exhibited an audible sub-stall
                                                // ("ji-ji-" gear strain sound). Mirrors PR #81
                                                // lower-bound rationale: stay 1° inside the
                                                // observed servo-strain boundary.
    static constexpr int RECOMMENDED_PITCH_MIN = 5;   // M5Stack official docs
    static constexpr int RECOMMENDED_PITCH_MAX = 85;  // M5Stack official docs

    // Issue #115: boot-time initialization target. Fall-safe neutral pose
    // well clear of both mechanical end-stops, in the centre of the
    // M5Stack-recommended 5..85° pitch range. Design follows the
    // goHome() pattern in m5stack/StackChan
    // (apps/app_setup/workers/servo.cpp:144) and the 1-second
    // positioning timing established in mongonta0716/stackchan-arduino
    // attachServos().
    //
    // Speed policy history (#121 Problem 2 -> #141 follow-ups):
    // - #121 Problem 2 originally raised BOOT_INIT_MOVE_MS from 1000 ms
    //   (the historical default that produced a startling "ブルンっ" boot
    //   motion) to 4000 ms (~11°/s on a 45° climb).
    // - Real-device verification then showed even 11°/s reads as
    //   perceptibly fast for the first boot-time servo motion, so the
    //   Phase 0 climb is pinned to an angular-speed cap of
    //   BOOT_INIT_TARGET_DEG_PER_SEC=15 deg/s. The WriteHeadAngles call
    //   sizes its duration from the actual yaw / pitch deltas so this
    //   cap holds on every axis. On the PMIC OFF/ON path Phase 0 stays
    //   a no-op of effect (the #138 safe-fallback seed makes start_deg
    //   == target_deg), so the BOOT_INIT_MOVE_MS budget simply elapses
    //   without WritePos movement.
    // The 100 ms post-settle vTaskDelay in InitializeServo() is
    // unchanged. The separate "unintended downward drop on power-on"
    // (#121 Problem 1) is addressed by the snap-suppress hold in
    // InitializeServo() Phase 1a (PR #137) and the ReadPos retry +
    // safe-fallback seed in this file (#138).
    //
    // Issue #138: promoted from local block scope inside
    // InitializeServo() to class-level static constexpr so the
    // safe-fallback branch in Phase 2 can seed
    // pitch_motion_.current_deg with BOOT_INIT_PITCH_DEG when the
    // pre-init ReadPos retries all fail. Without that seed, the
    // boot-init `WriteHeadAngles(0, 45, 4000)` interpolation would
    // start from the struct-default `current_deg=0` (== pos=620 at
    // deg=0, the lower mechanical end-stop) and walk WritePos calls
    // upward through end-stop-adjacent positions before reaching the
    // target, risking servo bus degradation if the SCS0009 wakes up
    // mid-sequence.
    static constexpr int BOOT_INIT_YAW_DEG = 0;
    // Rest/neutral pitch. Higher = look up (cf. PROX_REACT_PITCH_DEG=60 "slightly
    // up"). Lowered 45 -> 38 on user feedback: at 45 the neutral pose looked up
    // too much. This single constant drives boot-init, the proximity/touch revert
    // (TouchRevertCb) and the idle-settle return, so they stay consistent.
    static constexpr int BOOT_INIT_PITCH_DEG = 38;
    // BOOT_INIT_MOVE_MS=3000: minimum duration of the Phase 0 climb.
    // Used as a floor so the boot-init `WriteHeadAngles(0, 45, X)`
    // always elapses at least this long — required on the PMIC OFF/ON
    // path where the #138 safe-fallback seed makes Phase 0 a no-op of
    // effect, and the BOOT_INIT_MOVE_MS budget instead serves to span
    // the SCS0009 wake-up latency window so the post-init ReadPos
    // (Phase 0') lands well past it. The actual Phase 0 duration is
    // computed at call time from the current_deg → BOOT_INIT_*
    // deltas at BOOT_INIT_TARGET_DEG_PER_SEC=15 deg/s, then floored at this
    // constant; e.g. on the ESP32-only reset path with a yaw-90° prior
    // set-point, Phase 0 needs 6000 ms to honour the speed cap while a
    // yaw-0 prior is rounded up to this 3000 ms floor. This is the
    // no-stutter Smooth lower bound established under #121 Problem 2
    // (Issue #121 / PR #125 history: 1000 -> 4000 was a partial step
    // toward this; on-device feedback under #141 verification confirmed
    // 15 deg/s is the speed at which the ServoTask MOTION_TICK_MS=20 ms
    // interpolation stops being perceptible as individual position
    // jumps without sliding into a startling regime). Boot-time budget
    // is intentionally not optimised: operator safety and avoiding
    // mechanical stress take precedence over shaving seconds off the
    // initialization duration.
    //
    // On the PMIC OFF/ON path Phase 0 stays a no-op of effect
    // because the #138 safe-fallback seeds current_deg to
    // BOOT_INIT_PITCH_DEG, making start_deg == target_deg; the
    // BOOT_INIT_MOVE_MS budget then simply elapses without WritePos
    // movement.
    static constexpr uint32_t BOOT_INIT_MOVE_MS = 3000;
    // Single-source Phase 0 speed cap. 15 deg/s is the no-stutter Smooth
    // lower bound (#121 Problem 2 + #141 verification).
    static constexpr int BOOT_INIT_TARGET_DEG_PER_SEC = 15;

    // Runtime-tunable neutral (rest) pose. BOOT_INIT_YAW_DEG /
    // BOOT_INIT_PITCH_DEG remain the compile-time defaults; these members
    // hold the NVS-resolved values actually used by boot-init, the
    // proximity/touch revert (TouchRevertCb) and the idle-settle return.
    // Loaded from NVS (namespace "stackchan_pose") at the start of
    // InitializeServo() before the Phase 2 seed, written at runtime by the
    // self.robot.set_neutral_pose MCP tool. pitch is held to
    // [SAFE_PITCH_MIN, SAFE_PITCH_MAX] (clamped by set_neutral_pose before
    // storing, matching set_head_angles).
    int neutral_yaw_   = BOOT_INIT_YAW_DEG;
    int neutral_pitch_ = BOOT_INIT_PITCH_DEG;

    static int YawDegToPos(int deg) ;

    static int PitchDegToPos(int deg) ;

    static uint16_t clamp_u16(uint32_t v) ;

    // Map the StartMove duration contract to spring options that approximate
    // the requested timing. This is the stackchan-mcp side of
    // m5stack/StackChan's map_speed_to_spring_options(speed): shorter
    // duration -> higher stiffness/damping, longer duration -> lower
    // stiffness/damping, with critical damping for no overshoot.
    static smooth_ui_toolkit::SpringOptions_t MapDurationToSpringOptions(
        uint32_t duration_ms) ;

    enum class ReleaseReason : uint8_t {
        kManual = 0,
        kAutoIdle,
        kReengagement,
    };

    static const char* ReleaseReasonName(ReleaseReason reason) ;

    // Caller must hold scs_bus_mutex_.
    void PublishTorqueState() ;

    // Marks a fully-OFF transition while the bus write is still pending.
    // Returns this call's release epoch so auto-idle rollback can detect
    // another release publisher that interleaved after it.
    uint32_t MarkReleasing() ;

    // Block until torque_state_ leaves kReleasing or the elapsed-time
    // budget expires. Caller must NOT hold motion_mutex_ or scs_bus_mutex_.
    // Uses esp_timer_get_time() so the budget is honored at real time
    // regardless of CONFIG_FREERTOS_HZ.
    //
    // Returns true if the state is no longer kReleasing (proceed safely),
    // false if the wait budget was exhausted while still kReleasing
    // (caller decides how to handle: either skip with ESP_LOGW or defer to
    // its own bounded retry).
    bool WaitForKReleasingToClear() ;

    struct ServoTorqueResult {
        // -1 means "no bus frame was issued for this axis". In every
        // short-circuit path (idempotent_short_circuit or wait_exhausted)
        // the function returns before any EnableTorque() call, so both
        // bus-return fields keep this -1 default (Issue #171).
        int yaw_bus_return = -1;
        int pitch_bus_return = -1;
        bool yaw_ok = false;
        bool pitch_ok = false;
        // Issue #171: the old single `short_circuited` flag was overloaded
        // (set both for idempotent no-ops AND for wait-budget exhaustion),
        // so callers could not distinguish degraded-bus wait-exhaustion from
        // a legitimate no-op success. These two flags are orthogonal and
        // mutually exclusive: at most one is ever true.
        //   * idempotent_short_circuit: returned without a bus frame because
        //     the per-axis state already matched the request (success no-op).
        //   * wait_exhausted: returned without a bus frame because
        //     WaitForKReleasingToClear() hit its budget while still
        //     kReleasing (failure: the requested transition did not happen).
        bool idempotent_short_circuit = false;
        bool wait_exhausted = false;
    };

    class MotionDriver {
    public:
        virtual ~MotionDriver() = default;

        // Non-blocking. Both axes are dispatched within a single call.
        // WriteHeadAngles holds motion_mutex_ while calling this method; Tick()
        // and getters take the mutex internally for their own state access.
        virtual void StartMove(float yaw_deg, float pitch_deg,
                               uint32_t duration_ms,
                               bool prefer_linear = false) = 0;

        // Last-known committed angle for each axis.
        virtual float GetYawDeg() const = 0;
        virtual float GetPitchDeg() const = 0;

        // True iff at least one axis is currently in motion.
        virtual bool IsMoving() const = 0;

        // Called from ServoTask body at a driver-dependent cadence.
        virtual void Tick() = 0;

        // Optional hooks for drivers that need setup or shutdown.
        virtual bool Initialize() { return true; }
        virtual void Shutdown() {}

        // Invalidate the freshness token for one axis. Used by board-
        // level code that mutates AxisMotion fields directly outside
        // StartMove (currently InitializeServo's Phase 0' post-init
        // ReadPos re-sync, and the set_servo_torque MCP tool's
        // disable path). Caller must hold motion_mutex_.
        //
        // HostInterpolationMotionDriver: bumps the per-axis
        // request_token, defeating any post-bus freshness check from a
        // Tick() snapshot taken before the external mutation.
        //
        // ServoDelegatedMotionDriver: bumps the per-axis request_token
        // AND clears the corresponding AxisServo's per-axis private
        // cancellation state (pending_dispatch_, dispatch_failures_,
        // readmove_failures_), atomically with the caller's motion_mutex_
        // hold.
        //
        // Drivers without a token-based freshness guard treat this as
        // a no-op (default implementation). Argument is SERVO_YAW_ID
        // or SERVO_PITCH_ID; unknown values are ignored.
        virtual void InvalidateAxisToken(int /*axis_id*/) {}
    };

    class HostInterpolationMotionDriver final : public MotionDriver {
    public:
        HostInterpolationMotionDriver(ScsBus& scs_bus,
                                      SemaphoreHandle_t& scs_bus_mutex,
                                      SemaphoreHandle_t& motion_mutex,
                                      AxisMotion& yaw_motion,
                                      AxisMotion& pitch_motion)
            : scs_bus_(scs_bus),
              scs_bus_mutex_(scs_bus_mutex),
              motion_mutex_(motion_mutex),
              yaw_motion_(yaw_motion),
              pitch_motion_(pitch_motion),
              next_request_token_(0) {
            yaw_anim_.teleport(static_cast<float>(yaw_motion_.current_deg));
            pitch_anim_.teleport(static_cast<float>(pitch_motion_.current_deg));
        }

        void StartMove(float yaw_deg, float pitch_deg,
                       uint32_t duration_ms,
                       bool prefer_linear) override {
            uint32_t now_ms = static_cast<uint32_t>(esp_timer_get_time() / 1000);
            int yaw = static_cast<int>(yaw_deg);
            int pitch = static_cast<int>(pitch_deg);

            yaw_motion_.request_token = ++next_request_token_;
            yaw_motion_.target_deg = yaw;
            yaw_motion_.start_deg = yaw_motion_.current_deg;
            yaw_motion_.move_start_ms = now_ms;
            yaw_motion_.move_duration_ms = duration_ms;
            yaw_motion_.moving = (yaw_motion_.target_deg != yaw_motion_.current_deg);
            yaw_linear_mode_ = prefer_linear;

            pitch_motion_.request_token = ++next_request_token_;
            pitch_motion_.target_deg = pitch;
            pitch_motion_.start_deg = pitch_motion_.current_deg;
            pitch_motion_.move_start_ms = now_ms;
            pitch_motion_.move_duration_ms = duration_ms;
            pitch_motion_.moving = (pitch_motion_.target_deg != pitch_motion_.current_deg);
            pitch_linear_mode_ = prefer_linear;
            if (prefer_linear) {
                yaw_anim_.teleport(static_cast<float>(yaw_motion_.current_deg));
                yaw_snap_on_rest_ = false;
                pitch_anim_.teleport(static_cast<float>(pitch_motion_.current_deg));
                pitch_snap_on_rest_ = false;
                return;
            }

            smooth_ui_toolkit::SpringOptions_t spring_options =
                MapDurationToSpringOptions(duration_ms);
            StartAxisSpring(yaw_anim_, yaw_snap_on_rest_,
                            yaw_motion_.current_deg, yaw,
                            yaw_motion_.moving, spring_options);
            StartAxisSpring(pitch_anim_, pitch_snap_on_rest_,
                            pitch_motion_.current_deg, pitch,
                            pitch_motion_.moving, spring_options);
        }

        float GetYawDeg() const override {
            xSemaphoreTake(motion_mutex_, portMAX_DELAY);
            int yaw = yaw_motion_.current_deg;
            xSemaphoreGive(motion_mutex_);
            return static_cast<float>(yaw);
        }

        float GetPitchDeg() const override {
            xSemaphoreTake(motion_mutex_, portMAX_DELAY);
            int pitch = pitch_motion_.current_deg;
            xSemaphoreGive(motion_mutex_);
            return static_cast<float>(pitch);
        }

        bool IsMoving() const override {
            xSemaphoreTake(motion_mutex_, portMAX_DELAY);
            bool moving = yaw_motion_.moving || pitch_motion_.moving;
            xSemaphoreGive(motion_mutex_);
            return moving;
        }

        void Tick() override {
            constexpr TickType_t kInterFrameGap = pdMS_TO_TICKS(10);

            vTaskDelay(pdMS_TO_TICKS(MOTION_TICK_MS));

            AxisMotion yaw_local;
            AxisMotion pitch_local;
            int new_yaw_current;
            bool new_yaw_moving;
            int new_pitch_current;
            bool new_pitch_moving;
            bool yaw_linear_mode;
            bool pitch_linear_mode;
            uint64_t now_us = static_cast<uint64_t>(esp_timer_get_time());
            float dt_s;
            // Spring mode follows real elapsed time so bus ACK latency or
            // mutex contention does not stretch animation time indefinitely.
            // Clamp deep preemption to avoid a single large lurch.
            if (last_tick_us_ == 0) {
                dt_s = static_cast<float>(MOTION_TICK_MS) / 1000.0f;
            } else {
                dt_s = static_cast<float>(now_us - last_tick_us_) / 1000000.0f;
                if (dt_s > 0.1f) {
                    dt_s = 0.1f;
                }
            }
            last_tick_us_ = now_us;
            uint32_t now_ms = static_cast<uint32_t>(now_us / 1000);

            xSemaphoreTake(motion_mutex_, portMAX_DELAY);
            yaw_local = yaw_motion_;
            pitch_local = pitch_motion_;
            yaw_linear_mode = yaw_linear_mode_;
            pitch_linear_mode = pitch_linear_mode_;
            if (!yaw_local.moving && !pitch_local.moving) {
                xSemaphoreGive(motion_mutex_);
                return;
            }
            new_yaw_current = yaw_local.current_deg;
            new_yaw_moving = yaw_local.moving;
            if (yaw_linear_mode) {
                AdvanceAxisLinear(yaw_local, now_ms,
                                  new_yaw_current, new_yaw_moving);
            } else {
                AdvanceAxisSpring(yaw_local, yaw_anim_, yaw_snap_on_rest_,
                                  dt_s, new_yaw_current, new_yaw_moving);
            }
            new_pitch_current = pitch_local.current_deg;
            new_pitch_moving = pitch_local.moving;
            if (pitch_linear_mode) {
                AdvanceAxisLinear(pitch_local, now_ms,
                                  new_pitch_current, new_pitch_moving);
            } else {
                AdvanceAxisSpring(pitch_local, pitch_anim_, pitch_snap_on_rest_,
                                  dt_s, new_pitch_current, new_pitch_moving);
            }
            xSemaphoreGive(motion_mutex_);

            // Known carve-out (#161): motion_mutex_ is released here
            // and re-acquired after the WritePos block. If StartMove
            // or InvalidateAxisToken (Phase 0' / torque disable) fires
            // inside this release window, the WritePos calls below
            // still send a stale interpolation step on the bus. The
            // post-bus request_token guard below then correctly skips
            // the current_deg / moving commit, but the physical
            // intermediate position has already been issued. The
            // pre-PR move_start_ms guard had the same surface; this PR
            // does not regress that behavior. Closing the pre-bus gate
            // is tracked separately under #161.
            xSemaphoreTake(scs_bus_mutex_, portMAX_DELAY);
            if (yaw_local.moving) {
                int yaw_pos = YawDegToPos(new_yaw_current);
                int r = scs_bus_.WritePos(SERVO_YAW_ID, yaw_pos, MOTION_PER_WRITE_TIME_MS, 0);
                if (!ServoWritePosOk(r)) {
                    ESP_LOGW(TAG, "Motion yaw WritePos failed: r=%d (deg=%d, pos=%d)",
                             r, new_yaw_current, yaw_pos);
                }
            }
            vTaskDelay(kInterFrameGap);
            if (pitch_local.moving) {
                int pitch_pos = PitchDegToPos(new_pitch_current);
                int r = scs_bus_.WritePos(SERVO_PITCH_ID, pitch_pos, MOTION_PER_WRITE_TIME_MS, 0);
                if (!ServoWritePosOk(r)) {
                    ESP_LOGW(TAG, "Motion pitch WritePos failed: r=%d (deg=%d, pos=%d)",
                             r, new_pitch_current, pitch_pos);
                }
            }
            xSemaphoreGive(scs_bus_mutex_);

            xSemaphoreTake(motion_mutex_, portMAX_DELAY);
            if (yaw_motion_.request_token == yaw_local.request_token) {
                yaw_motion_.current_deg = new_yaw_current;
            }
            if (!new_yaw_moving && yaw_motion_.target_deg == yaw_local.target_deg
                && yaw_motion_.request_token == yaw_local.request_token) {
                yaw_motion_.moving = false;
            }
            if (pitch_motion_.request_token == pitch_local.request_token) {
                pitch_motion_.current_deg = new_pitch_current;
            }
            if (!new_pitch_moving && pitch_motion_.target_deg == pitch_local.target_deg
                && pitch_motion_.request_token == pitch_local.request_token) {
                pitch_motion_.moving = false;
            }
            xSemaphoreGive(motion_mutex_);
        }

        // Bump the request token for the specified axis. Used by
        // InitializeServo's Phase 0' re-sync so a Tick() snapshot
        // taken before the re-sync no longer passes the post-bus
        // freshness guard (which would otherwise overwrite the just-
        // re-synced current_deg / moving state). Caller must hold
        // motion_mutex_; this method does not take it.
        void InvalidateAxisToken(int axis_id) override {
            if (axis_id == SERVO_YAW_ID) {
                yaw_motion_.request_token = ++next_request_token_;
            } else if (axis_id == SERVO_PITCH_ID) {
                pitch_motion_.request_token = ++next_request_token_;
            }
        }

    private:
        static void StartAxisSpring(
            smooth_ui_toolkit::AnimateValue& axis_anim,
            bool& snap_on_rest,
            int current_deg,
            int target_deg,
            bool moving,
            const smooth_ui_toolkit::SpringOptions_t& spring_options) {
            if (!moving) {
                axis_anim.teleport(static_cast<float>(current_deg));
                snap_on_rest = false;
                return;
            }

            axis_anim.springOptions() = spring_options;
            axis_anim.teleport(static_cast<float>(current_deg));
            axis_anim = static_cast<float>(target_deg);
            snap_on_rest = true;
        }

        static void AdvanceAxisSpring(
            const AxisMotion& axis_local,
            smooth_ui_toolkit::AnimateValue& axis_anim,
            bool& snap_on_rest,
            float dt_s,
            int& new_current_deg,
            bool& new_moving) {
            if (!axis_local.moving) {
                return;
            }

            axis_anim.updateWithDelta(dt_s);
            new_current_deg = static_cast<int>(axis_anim.directValue());
            if (axis_anim.done()) {
                new_moving = false;
                if (snap_on_rest) {
                    new_current_deg = static_cast<int>(axis_anim.end);
                    snap_on_rest = false;
                }
            }
        }

        static void AdvanceAxisLinear(
            const AxisMotion& axis_local,
            uint32_t now_ms,
            int& new_current_deg,
            bool& new_moving) {
            if (!axis_local.moving) {
                return;
            }

            // Linear mode intentionally stays wall-clock based, matching the
            // pre-spring interpolation path used for boot-init slow climbs;
            // spring mode uses the real elapsed Tick delta instead.
            uint32_t elapsed = now_ms - axis_local.move_start_ms;
            if (axis_local.move_duration_ms == 0 ||
                elapsed >= axis_local.move_duration_ms) {
                new_current_deg = axis_local.target_deg;
                new_moving = false;
            } else {
                int delta = axis_local.target_deg - axis_local.start_deg;
                new_current_deg = axis_local.start_deg +
                    static_cast<int>(
                        static_cast<int64_t>(delta) * elapsed /
                        axis_local.move_duration_ms);
            }
        }

        ScsBus& scs_bus_;
        SemaphoreHandle_t& scs_bus_mutex_;
        SemaphoreHandle_t& motion_mutex_;
        AxisMotion& yaw_motion_;
        AxisMotion& pitch_motion_;
        // Monotonically increasing request id. Each StartMove increments
        // this and writes the new value into yaw_motion_.request_token
        // and pitch_motion_.request_token. Tick() snapshots both fields
        // with the rest of AxisMotion and uses request_token equality
        // (rather than move_start_ms, which only has ms resolution) to
        // detect whether a snapshot is still the live request.
        // InvalidateAxisToken() also bumps this counter for board-level
        // direct AxisMotion resets that do not go through StartMove
        // (currently InitializeServo's Phase 0' post-init ReadPos
        // re-sync and the set_servo_torque disable path); without that
        // bump, a Tick() snapshot taken before such a reset would pass
        // the post-bus freshness guard and overwrite the just-reset
        // state. motion_mutex_ guards this counter. The pre-bus
        // stale-WritePos race, where an external reset lands after the
        // snapshot but before the bus frame, is tracked separately
        // under #161.
        uint64_t next_request_token_ = 0;
        smooth_ui_toolkit::AnimateValue yaw_anim_;
        smooth_ui_toolkit::AnimateValue pitch_anim_;
        bool yaw_snap_on_rest_ = false;
        bool pitch_snap_on_rest_ = false;
        bool yaw_linear_mode_ = false;
        bool pitch_linear_mode_ = false;
        uint64_t last_tick_us_ = 0;
    };

    class ServoDelegatedMotionDriver final : public MotionDriver {
    public:
        ServoDelegatedMotionDriver(ScsBus& scs_bus,
                                   SemaphoreHandle_t& scs_bus_mutex,
                                   SemaphoreHandle_t& motion_mutex,
                                   AxisMotion& yaw_motion,
                                   AxisMotion& pitch_motion)
            : motion_mutex_(motion_mutex),
              yaw_motion_(yaw_motion),
              pitch_motion_(pitch_motion),
              next_request_token_(0),
              yaw_axis_(SERVO_YAW_ID, YawDegToPos, "yaw", yaw_motion,
                        scs_bus, scs_bus_mutex, motion_mutex,
                        next_request_token_,
                        /* post_dispatch_quiet_gap_ms = */ 10),
              pitch_axis_(SERVO_PITCH_ID, PitchDegToPos, "pitch",
                          pitch_motion, scs_bus, scs_bus_mutex,
                          motion_mutex, next_request_token_,
                          /* post_dispatch_quiet_gap_ms = */ 0) {}

        // StartMove only mutates AxisMotion state (under the caller's
        // motion_mutex_ per the MotionDriver::StartMove contract) and marks
        // each non-noop axis as having a pending dispatch. The actual WritePos
        // is performed by Tick() on the servo_motion task, so callers running
        // on timer tasks (e.g. TouchPollCb -> StartServoWobble) never block
        // on UART I/O. This mirrors HostInterpolationMotionDriver's
        // "StartMove writes state; Tick drives the bus" split and keeps
        // timer-task latency bounded.
        void StartMove(float yaw_deg, float pitch_deg,
                       uint32_t duration_ms,
                       bool prefer_linear) override {
            // The delegated path is already duration-bounded by the SCS0009
            // internal interpolation time argument; there is no host-side
            // profile to switch.
            (void)prefer_linear;
            uint16_t clamped = clamp_u16(duration_ms);
            if (duration_ms > clamped) {
                static bool duration_overflow_warned = false;
                if (!duration_overflow_warned) {
                    ESP_LOGW(TAG,
                             "Servo-delegated motion duration overflow: axis=yaw/pitch requested_ms=%u clamped_ms=%u",
                             (unsigned)duration_ms, (unsigned)clamped);
                    duration_overflow_warned = true;
                } else {
                    ESP_LOGD(TAG,
                             "Servo-delegated motion duration overflow: axis=yaw/pitch requested_ms=%u clamped_ms=%u",
                             (unsigned)duration_ms, (unsigned)clamped);
                }
            }

            // Per-axis no-op detection: when the axis is idle AND the request
            // matches the last-known current_deg AND the position is not
            // marked unknown, skip staging a dispatch. Issuing WritePos in
            // that case would start a delegated motion toward host-side
            // current_deg, which may diverge from the physical position
            // (e.g. after a boot ReadPos-failure path where current_deg was
            // seeded to BOOT_INIT_* via the safe fallback but the head sits
            // elsewhere). HostInterpolation path keeps its always-WritePos
            // behaviour for backward compatibility.
            //
            // position_unknown is the recovery signal from a prior ReadMove
            // force-clear: current_deg holds the requested target but
            // physical completion was never confirmed, so we MUST re-dispatch
            // (even when target == current_deg) to surface a persistent
            // failure rather than silently treat the axis as at-target.
            //
            // motion_mutex_ is held by the WriteHeadAngles caller per the
            // MotionDriver::StartMove contract (declared at the base class).
            // Taking it again here would deadlock the non-recursive FreeRTOS
            // semaphore — Stage() reads its AxisMotion directly.
            yaw_axis_.Stage(static_cast<int>(yaw_deg), clamped);
            pitch_axis_.Stage(static_cast<int>(pitch_deg), clamped);
        }

        float GetYawDeg() const override {
            xSemaphoreTake(motion_mutex_, portMAX_DELAY);
            int yaw = yaw_motion_.current_deg;
            xSemaphoreGive(motion_mutex_);
            return static_cast<float>(yaw);
        }

        float GetPitchDeg() const override {
            xSemaphoreTake(motion_mutex_, portMAX_DELAY);
            int pitch = pitch_motion_.current_deg;
            xSemaphoreGive(motion_mutex_);
            return static_cast<float>(pitch);
        }

        bool IsMoving() const override {
            xSemaphoreTake(motion_mutex_, portMAX_DELAY);
            bool moving = yaw_motion_.moving || pitch_motion_.moving;
            xSemaphoreGive(motion_mutex_);
            return moving;
        }

        void Tick() override {
            vTaskDelay(pdMS_TO_TICKS(MOTION_POLL_INTERVAL_MS));

            // Per-axis update. Post-bus-frame quiet period is held INSIDE
            // each axis's Dispatch() (WritePos) and PollReadMove()
            // (ReadMove) atomically with the bus frame itself (no release
            // / reacquire window where concurrent MCP callers could inject
            // bus frames). yaw is configured with 10 ms via
            // post_dispatch_quiet_gap_ms_ (the member name pre-dates the
            // post-ReadMove path but the value applies symmetrically to
            // both frame types); pitch is configured with 0 ms matching
            // the PR #146 empirical model.
            //
            // Inter-axis quiet period coverage:
            // - yaw Dispatch tick (WritePos): in-Dispatch 10 ms hold
            //   provides inter-axis spacing before pitch_axis_.Update().
            // - yaw PollReadMove tick (ReadMove): in-PollReadMove 10 ms
            //   hold provides the same inter-axis spacing — prevents the
            //   ReadMove -> WritePos 0 ms inter-frame sequence on the
            //   shared SCS bus that Phase 2's per-axis grain would
            //   otherwise expose (PR #146 had no such ordering because
            //   dispatch and poll Tick phases were mutually exclusive).
            // - yaw no-op tick: no yaw bus frame, no quiet period needed;
            //   pitch_axis_.Update() runs immediately.
            //
            // Two-axis simultaneous dispatch (the necessary side of the
            // PR #146 E2 / E4-cumulative hang trigger) remains
            // structurally eliminated: yaw and pitch each take
            // scs_bus_mutex_ in their own separate short-hold critical
            // section inside Update(), and pitch_axis_.Update() runs
            // only after yaw_axis_.Update() returns (i.e. after yaw's
            // scs_bus_mutex_ hold has been released).
            //
            // No inter-axis vTaskDelay at this wrapper level: every yaw
            // bus-frame-emitting branch already provides the 10 ms
            // wall-clock spacing inside its in-Method hold. The 10 ms
            // inter-frame budget also remains in the unchanged
            // HostInterpolationMotionDriver::Tick path.
            yaw_axis_.Update();
            pitch_axis_.Update();
        }

        // Caller holds motion_mutex_. Bumps next_request_token_ for the
        // specified axis (mirrors HostInterpolationMotionDriver's
        // implementation) AND clears the per-AxisServo private
        // cancellation state via OnExternalReset(), so that a Stage()
        // call that preceded the external mutation does not leak a stale
        // WritePos onto the bus from the next Update() tick.
        //
        // Used by InitializeServo's Phase 0' post-init ReadPos re-sync
        // and by the set_servo_torque MCP tool's disable path.
        //
        // Closing this cancellation boundary required a paired AxisMotion
        // (visible) + AxisServo (driver-private) atomic reset: bumping
        // the visible request_token alone (the default no-op fallback
        // this driver used to inherit) was insufficient because
        // pending_dispatch_ / dispatch_failures_ / readmove_failures_
        // survived a Phase 0' direct AxisMotion mutation and the next
        // Update() tick would re-issue a WritePos for the stale staged
        // target. Issue #160 tracks the design discussion and the
        // adversarial review that converged on this Option A design.
        //
        // Scope note: this closes the post-Update / next-tick race only.
        // A snapshot taken by Update() BEFORE the external reset still
        // carries a local `dispatch = true` boolean (Update()'s local
        // variable, not the member field) that survives this member-
        // state clear. The in-flight Dispatch() then passes the
        // request_token freshness gate at the pre-WritePos check (which
        // now sees a bumped token) and skips the WritePos itself, so
        // the bus is not touched with a stale frame — but the call
        // still acquires scs_bus_mutex_ once before returning. The
        // pre-bus stale-command race (Issue #161) is the remaining
        // cancellation-boundary layer and is intentionally NOT closed
        // by this PR.
        void InvalidateAxisToken(int axis_id) override {
            if (axis_id == SERVO_YAW_ID) {
                yaw_motion_.request_token = ++next_request_token_;
                yaw_axis_.OnExternalReset();
            } else if (axis_id == SERVO_PITCH_ID) {
                pitch_motion_.request_token = ++next_request_token_;
                pitch_axis_.OnExternalReset();
            }
        }

    private:
        static constexpr int kReadMoveFailureLimit = 5;
        static constexpr int kDispatchFailureLimit = 5;
        // Settle margin past move_duration_ms before treating a
        // stuck-high ReadMove (servo returns 1 forever after the
        // requested completion) as a failure. Without this bound,
        // a degraded servo / register path would keep moving=true
        // indefinitely; wobble would never advance and same-target
        // recovery would never run.
        static constexpr uint32_t kReadMoveStuckMarginMs = 1000;
        // Margin below move_duration_ms in which an early ReadMove==0
        // is allowed as a genuine completion (the SCS0009's internal
        // interpolation can finish slightly early). A ReadMove==0
        // arriving further before the commanded completion is
        // implausible and likely a stuck-low / false-zero status read
        // from a degraded register path; treat it as suspicious and
        // mark position_unknown so the next StartMove forces a fresh
        // dispatch instead of trusting the stale optimistic commit.
        static constexpr uint32_t kReadMoveEarlyMarginMs = 200;

        class AxisServo {
            // Lock-order audit for Update():
            // - Snapshot: motion_mutex_ only.
            // - Dispatch freshness gate: scs_bus_mutex_ -> motion_mutex_;
            //   motion_mutex_ is released before WritePos.
            // - Dispatch commit: motion_mutex_ only after scs_bus_mutex_ is
            //   released.
            // - ReadMove poll: scs_bus_mutex_ only, then motion_mutex_ only
            //   for commit. No path takes motion_mutex_ before scs_bus_mutex_.

        public:
            AxisServo(uint8_t servo_id, int (*deg_to_pos)(int),
                      const char* axis_name, AxisMotion& motion,
                      ScsBus& scs_bus, SemaphoreHandle_t& scs_bus_mutex,
                      SemaphoreHandle_t& motion_mutex,
                      uint64_t& next_request_token,
                      uint32_t post_dispatch_quiet_gap_ms)
                : servo_id_(servo_id),
                  deg_to_pos_(deg_to_pos),
                  axis_name_(axis_name),
                  motion_(motion),
                  scs_bus_(scs_bus),
                  scs_bus_mutex_(scs_bus_mutex),
                  motion_mutex_(motion_mutex),
                  next_request_token_(next_request_token),
                  post_dispatch_quiet_gap_ms_(post_dispatch_quiet_gap_ms) {}

            void Stage(int target_deg, uint16_t duration_ms) {
                // motion_mutex_ is held by the WriteHeadAngles caller per
                // the MotionDriver::StartMove contract. Taking it again here
                // would deadlock the non-recursive FreeRTOS semaphore.
                bool noop =
                    !motion_.moving && !motion_.position_unknown &&
                    target_deg == motion_.current_deg;
                if (noop) {
                    return;
                }

                uint32_t now_ms =
                    static_cast<uint32_t>(esp_timer_get_time() / 1000);
                motion_.start_deg = motion_.current_deg;
                motion_.target_deg = target_deg;
                motion_.move_start_ms = now_ms;
                // dispatch_start_ms stays 0 until FinishDispatch confirms
                // a WritePos ACK. ApplyReadMoveResult's stuck-high timeout
                // skips the check while dispatch_start_ms is 0, so retry
                // latency does not eat into the servo-internal duration
                // budget.
                motion_.dispatch_start_ms = 0;
                motion_.move_duration_ms = duration_ms;
                motion_.moving = true;
                motion_.request_token = ++next_request_token_;
                // NOTE: position_unknown is NOT cleared here. It is cleared
                // by FinishDispatch only after a successful WritePos ACK.
                // Clearing it on stage would let dispatch retry exhaustion
                // leave position_unknown=false despite no confirmed physical
                // motion; then the next same-target StartMove would no-op
                // skip on current_deg==target_deg and hide the bus failure.
                pending_dispatch_ = true;
                // Fresh request: reset the per-axis dispatch retry budget so
                // previous failures do not shorten this request's runway.
                dispatch_failures_ = 0;
            }

            // Returns true if this tick emitted any bus frame on the SCS
            // bus (WritePos via Dispatch() or ReadMove via PollReadMove()).
            // The wrapper Tick() does not use the bool to apply any
            // additional hold — both Dispatch() and PollReadMove() each
            // hold scs_bus_mutex_ atomically across their bus frame AND
            // the post-frame post_dispatch_quiet_gap_ms_ (yaw: 10 ms,
            // pitch: 0 ms), so the inter-axis quiet period is enforced
            // inside each axis's method without any release/reacquire
            // window. The return value is informational (kept for
            // diagnostic clarity and potential future use).
            bool Update() {
                AxisMotion snapshot;
                bool dispatch = false;
                xSemaphoreTake(motion_mutex_, portMAX_DELAY);
                snapshot = motion_;
                dispatch = pending_dispatch_;
                // Do NOT clear pending_dispatch here. FinishDispatch consumes
                // it only on success or retry exhaustion; transient WritePos
                // failures keep it true so the next tick retries the same
                // target instead of silently dropping the request.
                xSemaphoreGive(motion_mutex_);

                // With per-axis Update(), dispatch-vs-poll is chosen per
                // servo. One axis can spend this tick dispatching while the
                // other axis polls ReadMove after the wrapper's inter-axis
                // wall-clock gap.
                if (dispatch) {
                    return Dispatch(snapshot);
                }
                if (snapshot.moving) {
                    PollReadMove(snapshot);
                }
                return false;
            }

            // Caller must hold motion_mutex_. Clears the per-axis private
            // cancellation state so that a subsequent Update() tick observes
            // a clean slate after the board-level code directly mutates
            // AxisMotion outside the Stage() path (currently
            // InitializeServo's Phase 0' post-init ReadPos re-sync and the
            // set_servo_torque MCP tool's disable path).
            //
            // Does NOT take any semaphore (motion_mutex_ is already held by
            // the caller; double-take of the non-recursive FreeRTOS
            // semaphore would deadlock). Does NOT touch the SCS bus. Does
            // NOT touch motion_ (AxisMotion); the caller has already mutated
            // it before invoking this method through
            // ServoDelegatedMotionDriver::InvalidateAxisToken.
            //
            // INVARIANT: every new AxisServo private cancellation-state
            // field added in the future MUST be added to this reset.
            // Otherwise external resets (Phase 0' / torque disable / any
            // future cancellation caller) will leave stale state that the
            // next Update() may act on, regressing the Issue #160 fix.
            void OnExternalReset() {
                pending_dispatch_ = false;
                dispatch_failures_ = 0;
                readmove_failures_ = 0;
            }

        private:
            // Returns true if WritePos was actually issued on the bus
            // (i.e. the snapshot was still the live request at the
            // pre-WritePos freshness gate). Returns false when a newer
            // StartMove superseded the snapshot between Update's
            // motion_mutex_ release and Dispatch's freshness gate — in
            // that case the bus was not touched, and the wrapper Tick()
            // does not need to hold scs_bus_mutex_ across the
            // inter-frame gap.
            bool Dispatch(const AxisMotion& snapshot) {
                int result = 0;
                bool live = false;
                int pos = deg_to_pos_(snapshot.target_deg);
                uint16_t duration =
                    static_cast<uint16_t>(snapshot.move_duration_ms);

                xSemaphoreTake(scs_bus_mutex_, portMAX_DELAY);
                xSemaphoreTake(motion_mutex_, portMAX_DELAY);
                live = motion_.request_token == snapshot.request_token;
                xSemaphoreGive(motion_mutex_);
                if (live) {
                    result = scs_bus_.WritePos(servo_id_, pos, duration, 0);
                    // Hold scs_bus_mutex_ across the post-WritePos quiet
                    // period atomically with the WritePos itself. Without
                    // this, releasing the mutex here would expose a
                    // release/reacquire window where concurrent MCP
                    // callers (get_head_angles ReadPos, uart_diag raw
                    // frames) could acquire the bus and inject traffic
                    // before any wrapper-level quiet-period guard
                    // starts. The original PR #146 bundled critical
                    // section incidentally protected this window;
                    // Phase 2's per-axis short-hold grain restores it
                    // per axis instead. Skipped when the quiet gap is
                    // 0 ms (pitch axis) or the WritePos was superseded
                    // (!live) — see post_dispatch_quiet_gap_ms_ member
                    // comment for per-axis policy rationale.
                    if (post_dispatch_quiet_gap_ms_ > 0) {
                        vTaskDelay(pdMS_TO_TICKS(post_dispatch_quiet_gap_ms_));
                    }
                }
                xSemaphoreGive(scs_bus_mutex_);

                bool write_ok = !live || ServoWritePosOk(result);
                if (live && !write_ok) {
                    ESP_LOGW(TAG,
                             "Motion %s WritePos failed: r=%d (deg=%d, pos=%d)",
                             axis_name_, result, snapshot.target_deg, pos);
                }

                // dispatch_now_ms captures the time WritePos completed
                // (ACK or timeout). FinishDispatch uses this for
                // dispatch_start_ms on success, so ApplyReadMoveResult
                // measures from physical acceptance rather than staging.
                uint32_t dispatch_now_ms =
                    static_cast<uint32_t>(esp_timer_get_time() / 1000);

                xSemaphoreTake(motion_mutex_, portMAX_DELAY);
                // Commit / consume pending only if the snapshot is still
                // live and the WritePos actually ran. Superseded dispatches
                // keep the new request's pending flag intact and reset only
                // the stale retry counter.
                if (live && motion_.request_token == snapshot.request_token) {
                    FinishDispatch(snapshot.target_deg, write_ok,
                                   dispatch_now_ms);
                } else {
                    dispatch_failures_ = 0;
                }
                xSemaphoreGive(motion_mutex_);

                return live;
            }

            void PollReadMove(const AxisMotion& snapshot) {
                int read_move = -1;
                xSemaphoreTake(scs_bus_mutex_, portMAX_DELAY);
                read_move = scs_bus_.ReadMove(servo_id_);
                // Hold scs_bus_mutex_ across the post-ReadMove quiet
                // period atomically with the ReadMove itself, mirroring
                // the post-WritePos pattern in Dispatch(). Without this,
                // releasing the mutex here would expose a window where
                // the wrapper Tick()'s subsequent pitch_axis_.Update()
                // could enter Dispatch() and issue a pitch WritePos
                // immediately, creating a yaw-ReadMove -> pitch-WritePos
                // sequence with effectively 0 ms inter-frame spacing on
                // the shared SCS bus. PR #146's bundled critical section
                // separated dispatch ticks from poll ticks (Tick step 1
                // vs step 2 mutually exclusive), so this ReadMove ->
                // WritePos ordering never arose; Phase 2's per-axis
                // grain makes it possible, so the guard is restored
                // per axis here. Skipped when post_dispatch_quiet_gap_ms_
                // is 0 (pitch axis); the member is shared between
                // post-WritePos and post-ReadMove paths because the
                // bus-quiet rationale is identical for both frame types.
                if (post_dispatch_quiet_gap_ms_ > 0) {
                    vTaskDelay(pdMS_TO_TICKS(post_dispatch_quiet_gap_ms_));
                }
                xSemaphoreGive(scs_bus_mutex_);

                uint32_t now_ms =
                    static_cast<uint32_t>(esp_timer_get_time() / 1000);

                xSemaphoreTake(motion_mutex_, portMAX_DELAY);
                // request_token guards against a newer StartMove racing in
                // between the Update snapshot and this commit; ms-resolution
                // move_start_ms can collide for back-to-back requests.
                if (motion_.request_token == snapshot.request_token) {
                    ApplyReadMoveResult(read_move, now_ms);
                }
                xSemaphoreGive(motion_mutex_);
            }

            // Finalises a dispatched WritePos. Caller holds motion_mutex_.
            // - write_ok==true: commit current_deg = target, consume the
            //   pending_dispatch flag, reset dispatch + ReadMove failure
            //   counters. ReadMove poll then tracks the in-flight delegated
            //   motion to completion.
            // - write_ok==false: keep pending_dispatch=true so the next tick
            //   retries the same target (transient ACK timeout / UART error
            //   should not silently drop the request). Bound the retry by
            //   kDispatchFailureLimit; when exhausted, log once, consume
            //   pending_dispatch, and clear moving so the axis returns to
            //   idle rather than spinning the retry loop forever.
            // readmove_failures_ is reset on success only; it tracks ReadMove
            // polling and is independent of WritePos ack semantics.
            void FinishDispatch(int target_deg, bool write_ok,
                                uint32_t dispatch_now_ms) {
                if (write_ok) {
                    motion_.current_deg = target_deg;
                    // dispatch_start_ms records when the servo actually
                    // received the GOAL_POSITION / GOAL_TIME write. This
                    // (not the staging timestamp in move_start_ms) is what
                    // ApplyReadMoveResult uses for the stuck-high timeout,
                    // so degraded-bus dispatch latency doesn't eat into
                    // the servo-internal duration budget.
                    motion_.dispatch_start_ms = dispatch_now_ms;
                    // Confirmed WritePos ACK supersedes any prior
                    // position_unknown mark. The new WritePos+ReadMove
                    // cycle is what proves (or fails to prove) the
                    // physical position.
                    motion_.position_unknown = false;
                    pending_dispatch_ = false;
                    dispatch_failures_ = 0;
                    readmove_failures_ = 0;
                    return;
                }
                dispatch_failures_++;
                if (dispatch_failures_ >= kDispatchFailureLimit) {
                    ESP_LOGW(TAG,
                             "Motion %s WritePos retries exhausted: current_deg=%d target_deg=%d; %d consecutive dispatch failures, abandoning request and marking position unknown",
                             axis_name_, motion_.current_deg,
                             motion_.target_deg, kDispatchFailureLimit);
                    pending_dispatch_ = false;
                    motion_.moving = false;
                    // A WritePos ACK timeout is NOT proof that the servo
                    // ignored the command — the command may have reached
                    // the servo while only the ACK/readback path failed.
                    // In that case the physical head has already moved to
                    // target_deg, but current_deg still holds the old
                    // value. Without position_unknown=true here, the next
                    // same-old-position StartMove would no-op-skip on the
                    // stale current_deg and silently drop the recovery
                    // request — exactly the degraded-bus condition this
                    // path is meant to handle. Mark unknown so a same-
                    // target retry forces a fresh dispatch and either
                    // confirms (FinishDispatch write_ok clears the flag)
                    // or surfaces another failure.
                    motion_.position_unknown = true;
                    dispatch_failures_ = 0;
                }
                // else: pending_dispatch stays true; next Update will retry the
                // same target (same start_deg / move_start_ms / move_duration_ms).
            }

            void ApplyReadMoveResult(int read_move, uint32_t now_ms) {
                if (read_move >= 0) {
                    readmove_failures_ = 0;
                    if (read_move == 0) {
                        // Sanity check against a stuck-low / false-zero
                        // status register: FinishDispatch optimistically
                        // committed current_deg to target_deg on WritePos
                        // ACK, so a transient ReadMove==0 returned before
                        // the servo could physically reach target would
                        // make the host treat the axis as "at target" and
                        // let the next same-target StartMove no-op-skip.
                        // If the elapsed time since confirmed dispatch is
                        // implausibly short relative to the commanded
                        // move_duration_ms (allowing kReadMoveEarlyMarginMs
                        // for genuine early arrival), treat the zero as
                        // suspicious and mark the position unknown.
                        if (motion_.dispatch_start_ms != 0 &&
                            motion_.move_duration_ms > kReadMoveEarlyMarginMs) {
                            uint32_t elapsed = now_ms - motion_.dispatch_start_ms;
                            uint32_t plausible_min =
                                motion_.move_duration_ms - kReadMoveEarlyMarginMs;
                            if (elapsed < plausible_min) {
                                ESP_LOGW(TAG,
                                         "Motion %s ReadMove=0 implausibly early: current_deg=%d target_deg=%d, elapsed=%ums but commanded duration=%ums (early margin=%ums); marking position unknown",
                                         axis_name_, motion_.current_deg,
                                         motion_.target_deg, (unsigned)elapsed,
                                         (unsigned)motion_.move_duration_ms,
                                         (unsigned)kReadMoveEarlyMarginMs);
                                motion_.moving = false;
                                motion_.position_unknown = true;
                                return;
                            }
                        }
                        motion_.moving = false;
                        return;
                    }
                    // read_move > 0: servo reports still moving. Bound the
                    // wait by move_duration_ms + kReadMoveStuckMarginMs to
                    // guard against a stuck-high ReadMove (the servo or
                    // register path degrades such that the motion-status
                    // bit never clears even after the requested completion
                    // time has elapsed).
                    //
                    // Elapsed is measured from dispatch_start_ms (when the
                    // servo actually received the command via a successful
                    // WritePos ACK), not from move_start_ms (staging time),
                    // so degraded-bus dispatch latency does not cause
                    // premature force-clear while the servo is genuinely
                    // still mid-motion. If dispatch_start_ms is still 0 the
                    // WritePos has not yet ACK'd; skip the timeout check
                    // until the dispatch is confirmed. Unsigned subtraction
                    // stays wrap-safe across the uint32 ms counter.
                    if (motion_.dispatch_start_ms == 0) {
                        return;
                    }
                    uint32_t elapsed = now_ms - motion_.dispatch_start_ms;
                    if (elapsed > motion_.move_duration_ms + kReadMoveStuckMarginMs) {
                        ESP_LOGW(TAG,
                                 "Motion %s ReadMove stuck-high: current_deg=%d target_deg=%d, %ums past commanded completion; marking position unknown and force-clearing moving",
                                 axis_name_, motion_.current_deg,
                                 motion_.target_deg,
                                 (unsigned)(elapsed - motion_.move_duration_ms));
                        motion_.moving = false;
                        motion_.position_unknown = true;
                    }
                    return;
                }

                readmove_failures_++;
                if (readmove_failures_ >= kReadMoveFailureLimit) {
                    ESP_LOGW(TAG,
                             "Motion %s ReadMove failed: current_deg=%d target_deg=%d; %d consecutive ReadMove failures, marking position unknown and force-clearing moving (next StartMove will re-dispatch even if target matches current_deg)",
                             axis_name_, motion_.current_deg,
                             motion_.target_deg, kReadMoveFailureLimit);
                    motion_.moving = false;
                    // Without this flag, a subsequent same-target StartMove
                    // would no-op-skip on current_deg==target_deg and the
                    // bus failure would stay hidden behind the optimistic
                    // commit. Marking the position unknown forces the next
                    // StartMove to re-dispatch and surface (or recover from)
                    // the underlying ReadMove fault.
                    motion_.position_unknown = true;
                    readmove_failures_ = 0;
                }
            }

            uint8_t servo_id_;
            int (*deg_to_pos_)(int);
            const char* axis_name_;
            AxisMotion& motion_;
            ScsBus& scs_bus_;
            SemaphoreHandle_t& scs_bus_mutex_;
            SemaphoreHandle_t& motion_mutex_;
            uint64_t& next_request_token_;
            // Post-bus-frame quiet period held INSIDE scs_bus_mutex_ on
            // a successful bus operation. Applies to BOTH frame types:
            // - Dispatch() WritePos: scs_bus_mutex_ is not released between
            //   the WritePos and this vTaskDelay.
            // - PollReadMove() ReadMove: scs_bus_mutex_ is not released
            //   between the ReadMove and this vTaskDelay.
            //
            // yaw is configured with 10 ms to preserve the SCS bus quiet
            // period that the original PR #146 bundled critical section
            // incidentally protected — for WritePos -> next-frame ordering
            // (PR #146 empirical model) AND for the new ReadMove ->
            // pitch-WritePos ordering introduced by Phase 2's per-axis
            // grain (PR #146 had no such ordering because dispatch and
            // poll Tick phases were mutually exclusive).
            //
            // pitch is configured with 0 ms because the PR #146 empirical
            // model (E1 / E4-fresh / E5 / E6 all clean) shows post-pitch
            // quiet was not required for bus stability. Set to 0 to
            // disable the per-axis post-frame hold entirely. Holding
            // scs_bus_mutex_ across a vTaskDelay is intentional here —
            // it blocks concurrent MCP bus callers (get_head_angles
            // ReadPos, uart_diag raw frames) for the quiet-period
            // duration, which is the explicit invariant being restored.
            //
            // Name retained as "post_dispatch_quiet_gap_ms_" for
            // historical continuity; semantically it is "post-bus-frame
            // quiet gap" and applies symmetrically to both WritePos and
            // ReadMove paths.
            uint32_t post_dispatch_quiet_gap_ms_;
            int readmove_failures_ = 0;
            bool pending_dispatch_ = false;
            int dispatch_failures_ = 0;
        };

        SemaphoreHandle_t& motion_mutex_;
        AxisMotion& yaw_motion_;
        AxisMotion& pitch_motion_;
        // Monotonically increasing request id. Each StartMove that stages
        // a dispatch picks ++next_request_token_ and writes it into the
        // corresponding AxisMotion::request_token. Tick() then uses
        // request_token equality (rather than move_start_ms, which only
        // has ms resolution) to detect whether a snapshot is still the
        // live request. motion_mutex_ guards this counter.
        uint64_t next_request_token_ = 0;
        AxisServo yaw_axis_;
        AxisServo pitch_axis_;
    };

    void InitializePowerSaveTimer() ;

    void InitializeI2c() ;

    void InitializePortAI2c() ;

    esp_err_t InitPortBWs2812(uint16_t led_count) ;

    void I2cDetect() ;

    void InitializeAxp2101() ;

    void InitializeAw9523() ;

    void PollTouchpad() ;

    void InitializeFt6336TouchPad() ;

    void InitializeSpi() ;

    void InitializeIli9342Display() ;

     void InitializeCamera() ;

    bool servo_ok_ = false;
    bool rgb_ok_ = false;
    static constexpr uint8_t RGB_LED_COUNT = 12;  // StackChan base has 12 WS2812C
    static constexpr uint8_t RGB_DATA_PIN  = 13;  // PY32 expander pin (not ESP32 GPIO)

    void InitializeIOExpander() ;

    // Helpers for the LED MCP tools below. Centralised so the parsing/
    // clamping logic isn't duplicated in three handlers.
    static uint8_t ClampByte(int v) ;

    static bool JsonByte(cJSON* item, uint8_t* out) ;

    // Pack one RGB888 sample into the {lo, hi} RGB565 pair the PY32
    // expects in its LED RAM.
    static void PackRgb565(uint8_t r, uint8_t g, uint8_t b, uint8_t out[2]) ;

    // 全 RGB LED を同じ色にする helper。 self.led.set_all MCP tool と同じ I2C 経路
    // (PY32 経由 WS2812)。 PollTouchpad のタッチフィードバック等、 MCP 以外の
    // 経路から LED を駆動するときに使う。 PY32 init 失敗時 (rgb_ok_ == false)
    // は no-op で安全に抜ける。
    void SetAllRgbLeds(uint8_t r, uint8_t g, uint8_t b) ;

    void InitializeServo() ;

    ServoTorqueResult InternalSetServoTorque(bool yaw_enabled,
                                             bool pitch_enabled,
                                             ReleaseReason reason,
                                             uint32_t expected_release_epoch =
                                                 0) ;

    void EnsureTorqueEngagedBeforeMove() ;

    bool TakeMotionMutexAfterTorqueEngaged() ;

    void MaybeAutoReleaseTorque() ;

    // ---- Phase 7: head-touch (Si12T) sensing + reaction ----------------

    // Convenience wrapper around the existing servo write path. Mirrors the
    // math used in the self.robot.set_head_angles MCP tool so that touch
    // reactions and explicit MCP calls produce identical motion.
    //
    // Issue #1: previously this issued WritePos(id, pos, 100, 0) directly,
    // which hung the SCS0009 bus on large-angle reversals (the second
    // servo's frame collided with the first servo still being driven).
    // Now it sets the target and lets the servo_motion task interpolate.
    //
    // The wobble-cancel + StartMove sequence runs under motion_mutex_ so a
    // concurrent ServoWobbleStepAdvance() on servo_motion task cannot pass
    // its active-load gate after this call has cleared
    // servo_wobble_active_ but before it dispatches the user-driven
    // target — which would let a stale wobble step overwrite the new
    // command.
    void WriteHeadAngles(int yaw_deg, int pitch_deg,
                         uint32_t duration_ms = MOTION_DEFAULT_DURATION_MS,
                         bool prefer_linear = false) ;

    void WriteHeadAngles(int yaw_deg, int pitch_deg, int speed_dps) ;

    // Servo wobble: yaw -A -> +A -> -A -> 0. Each step is dispatched only
    // after the active MotionDriver reports idle, so the delegated path never
    // overwrites an in-flight SCS0009 internal motion.
    //
    // The initial active check stays outside motion_mutex_ so idle ServoTask
    // ticks do not run the torque re-engagement path. The dispatch body runs
    // under motion_mutex_; without that hold, a concurrent non-wobble
    // WriteHeadAngles() could clear servo_wobble_active_ AFTER this function
    // has passed the active-load + IsMoving gate but BEFORE the switch reaches
    // dispatch, which would let a wobble step overwrite the user's freshly-
    // staged target. Holding the mutex makes "wobble-active check → idle check
    // → step dispatch" atomic w.r.t. any external StartMove() request.
    void ServoWobbleStepAdvance() ;

    void StartServoWobble() ;

    static void ServoTaskTrampoline(void* arg) ;

    void ServoTaskMain() ;

    // Schedule a single-shot revert to "idle" face REACTION_HOLD_MS later.
    // Re-arming overwrites any pending revert. Skipped while the avatar
    // has been hidden via set_avatar("off"), so a stale revert timer
    // does not re-cover the LCD after the user explicitly hid it.
    static void TouchRevertCb(void* arg) ;

    void ScheduleIdleRevert() ;

    // Long idle backstop. Fired once IDLE_SETTLE_MS after the last
    // face / head / LED activity (each such activity re-arms it via
    // ScheduleIdleSettle). Recenters the head, returns to idle and turns the
    // base LEDs off. One-shot: it does NOT re-arm itself, so a settled
    // stack-chan stays quiet until the next interaction.
    static void IdleSettleCb(void* arg) ;

    void OnIdleSettle() ;

    // Re-arm the idle backstop. Called by every face / head / LED activity so
    // the 60 s window is measured from the last interaction, not boot.
    void ScheduleIdleSettle() ;

    // Decode a 2-bit channel level from the Si12T Output1 byte.
    // 00 = no output, 01 = low, 10 = medium, 11 = high.
    static inline char Si12tChLevelChar(uint8_t raw, int ch) ;

    // Emit the touch event log line. press_zones/press_raw are the
    // rising-edge snapshot (= the touch the user actually made);
    // release_raw is whatever the sensor reports at the falling edge
    // (normally 0x00 — anything else hints at debounce / hysteresis quirks).
    // ch=%c%c%c%c spells CH1〜CH4 levels using 0/L/M/H. CH4 is unused on
    // stack-chan (the head has 3 zones), so anything non-0 on CH4 is a
    // wiring noise / EMI signature worth investigating.
    void LogTouchEvent(const char* event_name, uint64_t duration_ms) ;

    void HandleTap(uint64_t duration_ms) ;

    void HandleStroke(uint64_t duration_ms) ;

    // 200 ms periodic poll. Reads the sensor, applies a 2-sample debounce on
    // the OR of the three head zones, and emits TAP/STROKE on falling edges.
    static void TouchPollCb(void* arg) ;

    void TouchPollTick() ;

    void InitializeSi12tTouch() ;

    // ---- Phase C1: proximity (LTR-553) sensing + hand-wave reaction -------

    static const char* ProxModeToString(ProxMode mode) ;

    // Returns true and writes *out on a known mode string; false on unknown
    // input so the caller can reject without mutating any state.
    static bool StringToProxMode(const std::string& s, ProxMode* out) ;

    // Board-local reaction on a confirmed "hand near" rising edge, dispatched
    // by prox_mode_:
    //   reflex = look up front + happy face, auto-reverting to idle.
    //   listen = a tap-equivalent listen toggle: the 1st wave starts a listen,
    //            a 2nd wave while listening stops it and sends the recording.
    //            No head motion or expression change; mirrors the LCD-tap path
    //            including the brief LED feedback.
    // Mirrors HandleStroke(); WriteHeadAngles / Start/StopListening are all
    // safe to call from the ESP_TIMER_TASK poll callback.
    void HandleProximity(int ps_raw) ;

    static void ProximityPollCb(void* arg) ;

    // PROX_POLL_MS periodic poll. Reads the 11-bit PS value, requires
    // PROX_DEBOUNCE_SAMPLES consecutive over-threshold samples to confirm
    // detection, and fires the reflex on the rising edge only (a held hand
    // produces no new edge; PROX_COOLDOWN_MS additionally gates re-fires
    // from repeated waving). Detection-state changes are logged with the
    // raw PS value to support threshold calibration on real hardware.
    void ProximityPollTick() ;

    void InitializeLtr553Proximity() ;

    // Map a face name to AvatarSet's 0-indexed slot, or -1 if unknown.
    static int FaceNameToIndex(const char* face) ;

    // Map a mouth shape name to AvatarSet's 0-indexed slot, or -1 if unknown.
    // Indices match avatar_set_.GetMouth() order (closed/half/open/e/u).
    static int MouthShapeToIndex(const char* shape) ;

    // ---- Layered-mode image lookups (face / eyes / mouth) -----------------
    //
    // Resolution order for each axis:
    //   1. If a dynamic AvatarSet has been loaded in layered mode, use
    //      its GetFace / GetEyes / GetMouth lookup. nullptr from AvatarSet
    //      is treated as "not in this set"; we fall through to (2).
    //   2. Static const tables in avatar_images.h (placeholder by default;
    //      avatar_images.local.cc swaps in real art for static-art users).
    //
    // Matrix-mode rendering uses avatar_set_.GetMatrix() directly inside
    // RenderAvatarLocked() and bypasses these helpers entirely.

    const lv_image_dsc_t* FaceImageForIndex(int face_index) const ;

    const lv_image_dsc_t* EyesImageForIndex(int eyes_index) const ;

    const lv_image_dsc_t* MouthImageForIndex(int mouth_index) const ;

    // Central mode-aware renderer. Caller must hold the display lock.
    //
    // Layered mode: picks a single image from the axis selected by
    // active_layer_ (no firmware-side compositing).
    // Matrix mode: looks up the pre-composed (face, eyes, mouth) image from
    // the AvatarSet's matrix table.
    //
    // Returns false if the requested image is unavailable (e.g. AvatarSet
    // loaded in matrix mode but the index triple is out of range, or the
    // avatar lv_obj cannot be created yet because the screen tree isn't up).
    bool RenderAvatarLocked() ;

    // Re-raise the status / subtitle / route-badge overlays above avatar_img_
    // when they exist and are visible. Called after every move_foreground of
    // the avatar so the overlays are never buried by a repaint. Caller must
    // hold the display lock (only ever called from RenderAvatarLocked /
    // EnsureAvatarObject, both of which already do).
    void PromoteOverlaysLocked() ;

    // ---- Avatar fetch pending machinery (intent doc invariant #6) -------

    // Lazily create avatar_pending_lock_. Safe to call repeatedly.
    void EnsureAvatarPendingLock() ;

    // Record the request as pending if a fetch is currently in progress.
    // Returns true when the request was captured (caller should NOT proceed
    // with the live LVGL write); false when no fetch is active and the
    // caller should run its normal path.
    //
    // Each helper writes the relevant subset of avatar_pending_ — a later
    // call within the same fetch window wins (the user's most recent
    // intent is what we apply when the fetch completes). set_avatar(face)
    // and set_avatar("off") are mutually exclusive on the face axis, so
    // they clear each other; mouth and blink axes are independent.
    bool DeferAvatarFaceIfFetching(const char* face) ;

    bool DeferAvatarOffIfFetching() ;

    bool DeferAvatarMouthIfFetching(const char* shape) ;

    bool DeferAvatarBlinkIfFetching(bool enabled) ;

    // Drain avatar_pending_ and apply it. Called from the avatar_fetch
    // worker task after AvatarSet::AdoptOwnedBuffer returns (regardless of success);
    // the caller must have already cleared avatar_fetch_in_progress_ so
    // that the public SetAvatarExpression / SetMouthShape / set_blink
    // paths invoked here run their live LVGL writes instead of looping
    // back through the defer helpers.
    void ApplyPendingAvatarAfterFetch() ;

    // Create avatar_img_ on the active LVGL screen, scaled to fill the LCD.
    // Caller must hold the LVGL/display lock. Returns true on success or
    // when avatar_img_ already exists.
    bool EnsureAvatarObject() ;

    // Create status_label_ on the active LVGL screen, anchored near the top
    // centre and styled with a translucent black pill so the text stays
    // readable over any avatar frame. Caller must hold the display lock.
    // Returns true on success or when status_label_ already exists. Starts
    // hidden; SetStatusText() controls visibility.
    bool EnsureStatusLabel() ;

    // Public entry for the self.display.set_status_text MCP tool. An empty
    // string hides the label; any non-empty text shows it, brings it to the
    // foreground (above the avatar) and updates the caption. Visibility is
    // independent of the avatar layer, so set_avatar("off") does not clear
    // the status text. Safe to call from any task.
    bool SetStatusText(const char* text) ;

    // Create subtitle_label_ on the active LVGL screen, pinned to the bottom
    // centre and word-wrapped to a few lines so a spoken sentence stays
    // legible over the avatar. Caller must hold the display lock. Returns
    // true on success or when subtitle_label_ already exists. Starts hidden;
    // SetSubtitleText() controls visibility.
    bool EnsureSubtitleLabel() ;

    // Public entry for the self.display.set_subtitle MCP tool. An empty
    // string hides the subtitle; any non-empty text shows it, re-promotes it
    // above the avatar and updates the caption. Visibility is independent of
    // the avatar layer. Safe to call from any task.
    bool SetSubtitleText(const char* text) ;

    // Create route_badge_ on the active LVGL screen, pinned to the top-right
    // corner (clear of status_label_ at top-centre). Caller must hold the
    // display lock. Returns true on success or when route_badge_ already
    // exists. Starts hidden; SetRouteBadge() controls visibility.
    bool EnsureRouteBadge() ;

    // Public entry for the self.display.set_route_badge MCP tool. An empty
    // string hides the badge; any non-empty text shows it, re-promotes it
    // above the avatar and updates the caption (the gateway sends "H" while
    // Hermes is serving the turn). Safe to call from any task.
    bool SetRouteBadge(const char* text) ;

    // Apply the requested face to avatar_img_. Returns false if the face is
    // unknown or the avatar object cannot be created yet.
    bool SetAvatarExpressionLocked(const char* face) ;

    // Public-style entry that takes the display lock. Used by the MCP tool
    // and by the deferred init timer. Always safe to call from any task.
    //
    // Also handles the "resume from off" path: if the previous face was
    // "off" (avatar layer hidden), the layer is unhidden here, and if blink
    // was enabled before SetAvatarOff() ran it is restored automatically.
    bool SetAvatarExpression(const char* face) ;

    // Hide the avatar layer and disable blink so the underlying
    // xiaozhi-esp32 screens (WiFi config UI, OTA, settings) become visible.
    // The avatar lv_obj is kept allocated so a subsequent
    // SetAvatarExpression(<other face>) can re-show it cheaply, and the
    // previous blink state is remembered for restoration.
    bool SetAvatarOff() ;

    // Internal-only entry used by autonomous animations (touch reactions,
    // idle revert, etc.). Skips the face change while the avatar is in
    // the user-requested "off" state, so the underlying xiaozhi-esp32
    // screens remain visible. Returns true if the face was applied.
    bool SetAvatarExpressionIfActive(const char* face) ;

    // Schedule a one-shot/periodic timer that keeps trying to install the
    // initial avatar image until the LVGL screen tree is ready (i.e. after
    // Application::Start() has run Display::SetupUI()).
    void InitializeAvatar() ;

    // ---- Phase 2: parts (eyes / mouth) and blink state machine ----------
    //
    // Eye and mouth axes share the unified rendering state machine above —
    // each operation updates current_*_index_ + active_layer_ and asks
    // RenderAvatarLocked() to redraw. In layered mode that produces the
    // upstream Phase 2 behaviour (one image at a time, with blink
    // temporarily replacing the face); in matrix mode the same state
    // change drives a composite (face, eyes, mouth) frame.

    // Restore the resting expression after a part overlay (= blink end /
    // explicit stop). Eyes return to 0 (open); the mouth index is preserved
    // so a Phase 4 lip-sync shape kept after the sequence ends continues to
    // be composited in matrix mode and remains the last frame the next
    // mouth call will replace in layered mode.
    bool RestoreCurrentFaceLocked() ;

    // Public mouth setter: wraps lock + look-up.
    bool SetMouthShape(const char* shape) ;

    // Step callback for the four-phase blink sequence. Each invocation
    // advances blink_state_, applies the corresponding image, and re-arms
    // blink_step_timer_ unless we're returning to the resting face.
    static void BlinkStepCb(void* arg) ;

    void BlinkStepAdvance() ;

    // Schedule callback: fires roughly every BLINK_MIN_GAP_MS..BLINK_MAX_GAP_MS.
    // Starts a new blink if enabled and not already blinking, then re-arms
    // itself with a fresh random interval.
    static void BlinkScheduleCb(void* arg) ;

    void BlinkScheduleTick() ;

    void EnsureBlinkTimers() ;

    void StartBlinkTimer() ;

    void StopBlinkTimer() ;

    // ---- Phase 4 audio (Issue #76): TTS state-driven lip-sync ----------
    //
    // While the gateway is playing TTS audio (tts.start..tts.stop), cycle
    // the mouth shape through CLOSED -> HALF -> OPEN -> HALF on a fixed
    // TTS_LIPSYNC_STEP_MS cadence. This is the (A) state-driven approach
    // proposed in Issue #76; the (B) audio-envelope-driven follow-up will
    // replace this cycle with a per-frame amplitude mapping in a separate
    // change.
    //
    // Concurrency:
    //   - Single esp_timer self-rearming on ESP_TIMER_TASK.
    //   - Coexists with the mouth-sequence playback task: when
    //     mouth_seq_active_ is true (the user issued a set_mouth_sequence
    //     while we were animating), the lip-sync step yields its frame and
    //     re-arms; the user-issued sequence wins until it completes, then
    //     lip-sync resumes naturally on the next tick.
    //   - Pauses autonomous blink while active (same Phase 2 reasoning as
    //     the mouth-sequence task: BlinkStepAdvance()'s
    //     RestoreCurrentFaceLocked() would overwrite the mouth overlay).
    //     Restores blink at stop based on blink_desired_ so a set_blink
    //     issued mid-playback is honoured.
    static void TtsLipSyncStepCb(void* arg) ;

    void TtsLipSyncStepAdvance() ;

    void EnsureTtsLipSyncTimer() ;

    void StartTtsLipSync() ;

    void StopTtsLipSync() ;

    // ---- Phase 2: lip-sync sequence playback (Issue #5) ----------------
    //
    // set_mouth_sequence accepts a list of {shape, duration_ms} pairs and
    // walks through it on a dedicated FreeRTOS task. Each step swaps the
    // mouth-only image and waits duration_ms before advancing. Walking the
    // queue locally avoids the per-step WebSocket RTT jitter that callers
    // see when issuing many set_mouth calls back-to-back from a TTS loop.
    //
    // Concurrency model:
    //   - mouth_seq_lock_ protects mouth_seq_pending_.
    //   - mouth_seq_signal_ is a binary semaphore that wakes the task when
    //     a new sequence has been enqueued.
    //   - mouth_seq_active_ / mouth_seq_cancel_requested_ are volatile flags
    //     read by the task between steps. Setting cancel_requested while the
    //     task is sleeping in vTaskDelay simply means the task picks up the
    //     cancel at the next slice boundary (kMouthCancelSliceMs apart).
    //   - Re-entry semantics: a fresh set_mouth_sequence call replaces the
    //     pending queue and marks cancel_requested so the task drops the
    //     remainder of the current sequence and starts the new one.
    //   - Interrupt sources: set_mouth, set_avatar, and set_mouth_sequence
    //     all call RequestMouthSequenceCancel() before mutating display
    //     state, so a sequence in flight is cleanly preempted.
    //
    // Trade-offs:
    //   - The MCP Property type system does not support array values, so
    //     the gateway serialises `steps` to a JSON string and passes it as
    //     `steps_json`. Validation happens here once, atomically: if any
    //     step is malformed the whole call is rejected and nothing is
    //     queued (no half-played sequences).
    //   - Autonomous blink is paused while a sequence plays because the
    //     blink state machine ends by calling RestoreCurrentFaceLocked(),
    //     which would replace the active mouth overlay with the resting
    //     face image (see Phase 2 comment near BlinkStepAdvance()).
    //   - The final shape is held after the sequence finishes; callers
    //     that want the mouth to close at the end should append a
    //     {"closed", N} step explicitly. This keeps the primitive composable
    //     with future expression-style use cases (e.g. ending on an open
    //     smile).
    static constexpr int kMaxMouthSequenceSteps = 256;
    static constexpr int kMouthStepMinMs = 10;
    static constexpr int kMouthStepMaxMs = 10000;
    static constexpr uint32_t kMouthCancelSliceMs = 20;

    struct MouthStep {
        std::string shape;
        uint32_t duration_ms;
    };

    struct MouthSequenceEnqueueResult {
        bool ok;
        std::string error;
        int queued_steps;
        uint32_t total_duration_ms;
    };

    TaskHandle_t mouth_seq_task_ = nullptr;
    SemaphoreHandle_t mouth_seq_lock_ = nullptr;     // protects mouth_seq_pending_ + generation
    SemaphoreHandle_t mouth_seq_signal_ = nullptr;   // binary semaphore: wake the task
    std::vector<MouthStep> mouth_seq_pending_;
    std::atomic<bool> mouth_seq_active_{false};
    std::atomic<bool> mouth_seq_cancel_requested_{false};
    // Generation counter bumped under mouth_seq_lock_ by every preemption
    // path (set_mouth, set_avatar, fresh set_mouth_sequence). The playback
    // task latches a snapshot at sequence start and re-checks before every
    // SetMouthShape() call, so a preempt issued in the 0..kMouthCancelSliceMs
    // window between the last cancel-flag check and the next SetMouthShape
    // call still aborts the current frame draw. Without this, the task
    // could draw one stale mouth frame after the user-issued set_mouth /
    // set_avatar handler had already returned.
    std::atomic<uint32_t> mouth_seq_generation_{0};
    // User's explicitly-requested blink state, independent of whether
    // a mouth sequence is currently suppressing the timer. set_blink
    // updates this; the playback task restores StartBlinkTimer() at
    // sequence end iff this is true. Without this split a set_blink
    // call issued during a sequence is silently overwritten by the
    // pre-sequence snapshot when the task finishes.
    std::atomic<bool> blink_desired_{false};

    // Mark any in-flight or pending sequence for cancellation. Safe to
    // call from any thread. Takes mouth_seq_lock_ so that:
    //   - mouth_seq_pending_ is cleared atomically (callers that issue
    //     set_mouth / set_avatar in the brief window between
    //     EnqueueMouthSequence() returning and the task waking up don't
    //     get overwritten by the queued-but-not-yet-active sequence);
    //   - mouth_seq_cancel_requested_ is set so the task aborts at the
    //     next slice boundary if it is already active;
    //   - mouth_seq_generation_ is bumped under release ordering so the
    //     task observes a stale generation at its next per-step check
    //     and skips the remaining SetMouthShape() calls.
    // Idempotent under repeated calls.
    void RequestMouthSequenceCancel() ;

    // Parse and validate a JSON-serialised sequence, then atomically
    // replace mouth_seq_pending_ and signal the playback task. Returns
    // a populated MouthSequenceEnqueueResult; on validation failure
    // nothing is queued.
    MouthSequenceEnqueueResult EnqueueMouthSequence(const std::string& steps_json) ;

    static void MouthSequenceTaskTrampoline(void* arg) ;

    void MouthSequenceTaskLoop() ;

    void InitializeMouthSequenceTask() ;

    void RegisterMcpTools() ;

public:
    StackChanBoard() ;

    virtual AudioCodec* GetAudioCodec() override ;

    virtual Display* GetDisplay() override ;

    virtual Camera* GetCamera() override ;

    virtual bool GetBatteryLevel(int &level, bool& charging, bool& discharging) override ;

    virtual void SetPowerSaveLevel(PowerSaveLevel level) override ;

    // Phase 4 audio (Issue #76): drive avatar mouth animation alongside TTS
    // playback. The gateway's tts.start / tts.stop notifications reach this
    // board via Application::OnIncomingJson() -> Board::OnTtsStart/Stop().
    virtual void OnTtsStart() override ;

    virtual void OnTtsStop() override ;

    // Phase 4.5 avatar (saiverse-stackchan-addon): handle the gateway's
    // `avatar_set_fetch` WS message. Parse url/token/mode/checksum/
    // expected_size, spawn a worker task that performs HTTP GET + SHA256
    // verify + AvatarSet::AdoptOwnedBuffer, then send `avatar_set_loaded` back via
    // the protocol. Runs on the protocol receive task; the actual fetch
    // is delegated to a FreeRTOS task to avoid blocking the receive loop
    // while the LCD-sized payload flows in.
    virtual void OnAvatarSetFetch(const cJSON* root) override ;

    // ---- Phase 4.5 avatar helpers --------------------------------------

    struct AvatarFetchContext {
        StackChanBoard* board;
        std::string url;
        std::string token;
        AvatarSet::Mode mode;
        size_t expected_size;
        std::string expected_sha256;
    };

    static void AvatarFetchTaskTrampoline(void* arg) ;

    void RunAvatarFetch(const AvatarFetchContext* ctx) ;

    static void SendAvatarSetLoaded(
        bool ok, const std::string& checksum, const std::string& error_code) ;

    static void SendAvatarSetLoadedError(
        const std::string& checksum, const std::string& error_code) ;

    // --------------------------------------------------------------------

    virtual Backlight *GetBacklight() override ;
};

#endif  // STACKCHAN_BOARD_H
