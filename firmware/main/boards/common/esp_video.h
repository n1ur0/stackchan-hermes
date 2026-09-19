#pragma once
#include "sdkconfig.h"

#include <lvgl.h>
#include <thread>
#include <mutex>
#include <memory>
#include <vector>

#include <freertos/FreeRTOS.h>
#include <freertos/queue.h>

#include "camera.h"
#include "jpg/image_to_jpeg.h"
#include "esp_video_init.h"

struct JpegChunk {
    uint8_t* data;
    size_t len;
};

class EspVideo : public Camera {
private:
    struct FrameBuffer {
        uint8_t *data = nullptr;
        size_t len = 0;
        uint16_t width = 0;
        uint16_t height = 0;
        v4l2_pix_fmt_t format = 0;
    } frame_;
    v4l2_pix_fmt_t sensor_format_ = 0;
#ifdef CONFIG_XIAOZHI_ENABLE_ROTATE_CAMERA_IMAGE
    uint16_t sensor_width_ = 0;
    uint16_t sensor_height_ = 0;
#endif  // CONFIG_XIAOZHI_ENABLE_ROTATE_CAMERA_IMAGE
    int video_fd_ = -1;
    bool streaming_on_ = false;
    struct MmapBuffer { void *start = nullptr; size_t length = 0; };
    std::vector<MmapBuffer> mmap_buffers_;
    std::string explain_url_;
    std::string explain_token_;
    std::string tracking_url_;   // dedicated remote-tracker target (frame push)
    std::string tracking_token_;
    std::thread encoder_thread_;
    // Serializes the JPEG-encode + HTTP-upload critical section shared by
    // Explain() and PushFrameForTracking() (both reassign encoder_thread_).
    std::mutex encoder_mutex_;

public:
    EspVideo(const esp_video_init_config_t& config);
    ~EspVideo();

    virtual void SetExplainUrl(const std::string& url, const std::string& token) override;
    // Dedicated tracking target; when non-empty, PushFrameForTracking() POSTs here
    // instead of explain_url_ (gateway vision URL). Lets the frame-push path run a
    // remote tracker without colliding with the photo/Explain flow.
    virtual void SetTrackingUrl(const std::string& url, const std::string& token = "");
    virtual bool Capture();
    // Copy-after-capture access to the most recent frame for external vision
    // algorithms. All out-params point into / are owned by EspVideo and are
    // only valid until the next Capture(). Call Capture() first. Caller must
    // NOT free *data. Returns false if no frame has been captured yet.
    // *pix_fmt is a V4L2_PIX_FMT_* value (e.g. RGB565, RGB24, YUYV).
    virtual bool GetLastFrame(uint8_t** data, size_t* len, uint32_t* pix_fmt,
                              uint16_t* width, uint16_t* height) const;
    // Push-style capture for the on-device face tracker. Capture()s the
    // latest frame, JPEG-encodes it at the given (low) quality, and POSTs it
    // to explain_url_ as a multipart "file" field ONLY (no question field),
    // returning the server's raw response. Compiled out (returns "") unless
    // CONFIG_STACKCHAN_FRAME_PUSH=y. Uses the same encoder-thread + chunked
    // multipart path as Explain().
    virtual std::string PushFrameForTracking(int jpeg_quality = 65);
    // 翻转控制函数
    virtual bool SetHMirror(bool enabled) override;
    virtual bool SetVFlip(bool enabled) override;
    virtual std::string Explain(const std::string& question);
};
