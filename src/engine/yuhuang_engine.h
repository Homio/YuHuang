#ifndef YUHUANG_ENGINE_H
#define YUHUANG_ENGINE_H

#include <fcitx/inputmethodengine.h>
#include <fcitx/addonfactory.h>
#include <fcitx/addonmanager.h>
#include <fcitx/inputcontextproperty.h>
#include <fcitx/instance.h>
#include <fcitx-config/configuration.h>
#include <fcitx-config/option.h>
#include <string>
#include <memory>
#include <thread>
#include <mutex>
#include <atomic>
#include <functional>
#include <fcitx-utils/eventdispatcher.h>

namespace yuhuang {

// ===== 按键符号常量 =====
namespace vk {
    constexpr uint32_t Escape = 0xFF1B;
    constexpr uint32_t Return = 0xFF0D;
    constexpr uint32_t F5     = 0xFFC5;
    constexpr uint32_t F6     = 0xFFC6;
}

// ===== 配置类 (fcitx5-configtool GUI 可编辑) =====
// 注意: double/float 非 fcitx5 原生支持, 时间值用 int (毫秒) 存储
FCITX_CONFIGURATION(YuHuangConfig,

    // ---- 触发键 (PTT) ----
    fcitx::Option<fcitx::Key, fcitx::KeyConstrain> triggerKey{
        this, "TriggerKey", "Push-to-talk trigger key",
        fcitx::Key("Control_R"),
        fcitx::KeyConstrain(
            fcitx::KeyConstrainFlags{}
            | fcitx::KeyConstrainFlag::AllowModifierOnly
            | fcitx::KeyConstrainFlag::AllowModifierLess)
    };

    fcitx::Option<bool> checkConflicts{
        this, "CheckConflicts",
        "Warn if trigger key conflicts with system shortcuts", true
    };

    // ---- 后端连接 ----
    fcitx::Option<std::string> backendSocket{
        this, "BackendSocket",
        "Unix Domain Socket path for backend service",
        "/tmp/yuhuang-backend.sock"
    };

    // ---- 音频设备 ----
    fcitx::Option<std::string> audioDevice{
        this, "AudioDevice",
        "Microphone device name (leave empty for default, run 'yuhuang-ctl mic' to list)",
        ""
    };

    // ---- 语音参数 (毫秒存储, 使用时转换) ----
    fcitx::Option<int, fcitx::IntConstrain> vadSilenceTimeoutMs{
        this, "VADSilenceTimeout",
        "Silence timeout in milliseconds before segment finalization",
        800, fcitx::IntConstrain(100, 5000)
    };

    fcitx::Option<int, fcitx::IntConstrain> asrIntermediateInterval{
        this, "ASRIntermediateInterval",
        "Intermediate ASR result refresh interval (ms)",
        300, fcitx::IntConstrain(50, 2000)
    };

    // ---- LLM 优化 ----
    fcitx::Option<bool> llmEnabled{
        this, "LLMEnabled",
        "Enable LLM optimization (requires LLM backend)", false
    };

    fcitx::Option<std::string> llmBaseUrl{
        this, "LLMBaseUrl",
        "LLM API base URL (OpenAI-compatible)",
        "http://localhost:8000/v1"
    };

    fcitx::Option<std::string> llmApiKey{
        this, "LLMApiKey", "LLM API key", "token-abc123"
    };

    fcitx::Option<std::string> llmModel{
        this, "LLMModel", "LLM model name", "qwen2.5-7b-instruct"
    };

    fcitx::Option<int, fcitx::IntConstrain> llmOptimizeDelayMs{
        this, "LLMOptimizeDelay",
        "Delay before LLM optimization (milliseconds)",
        500, fcitx::IntConstrain(0, 5000)
    };

    fcitx::Option<int, fcitx::IntConstrain> llmAutoCommitDelayMs{
        this, "LLMAutoCommitDelay",
        "Delay before auto-commit after LLM (milliseconds)",
        200, fcitx::IntConstrain(0, 3000)
    };

    fcitx::Option<int, fcitx::IntConstrain> llmTemperature{
        this, "LLMTemperature",
        "LLM temperature (0-100, e.g. 30 = 0.30)",
        30, fcitx::IntConstrain(0, 100)
    };

    fcitx::Option<int, fcitx::IntConstrain> llmMaxTokens{
        this, "LLMMaxTokens", "LLM max output tokens", 2000,
        fcitx::IntConstrain(64, 8192)
    };
)

// ===== Unix Socket 客户端 (与后端通信) =====
class BackendClient {
public:
    explicit BackendClient(const std::string &socketPath);
    ~BackendClient();

    bool connect();
    void disconnect();
    bool isConnected() const;

    bool sendAudio(const std::vector<int16_t> &pcmData);
    bool sendCommand(const std::string &command);

    using ResultCallback = std::function<void(const std::string &type,
                                              const std::string &text)>;
    void setResultCallback(ResultCallback callback);

    void startReceiveLoop();
    void stopReceiveLoop();

    int fd() const { return fd_; }

private:
    std::string socketPath_;
    int fd_ = -1;
    std::atomic<bool> connected_{false};
    std::atomic<bool> receiving_{false};
    std::thread receiveThread_;
    std::mutex mutex_;
    ResultCallback callback_;

    void receiveLoop();
    bool sendRaw(const uint8_t *data, size_t len);
};

// ===== 每个 InputContext 的状态 =====
class YuHuangState : public fcitx::InputContextProperty {
public:
    YuHuangState(class YuHuangEngine *engine, fcitx::InputContext *ic);
    ~YuHuangState();

    void updatePreedit(const std::string &text);
    void commitText(const std::string &text);
    void reset();

    fcitx::InputContext *inputContext() const { return ic_; }

private:
    YuHuangEngine *engine_;
    fcitx::InputContext *ic_;
};

// ===== 输入法引擎主类 =====
class YuHuangEngine : public fcitx::InputMethodEngineV2 {
public:
    explicit YuHuangEngine(fcitx::Instance *instance);
    ~YuHuangEngine();

    // InputMethodEngine 接口
    void keyEvent(const fcitx::InputMethodEntry &entry,
                  fcitx::KeyEvent &keyEvent) override;

    void activate(const fcitx::InputMethodEntry &entry,
                  fcitx::InputContextEvent &event) override;

    void deactivate(const fcitx::InputMethodEntry &entry,
                    fcitx::InputContextEvent &event) override;

    void reset(const fcitx::InputMethodEntry &entry,
               fcitx::InputContextEvent &event) override;

    // 配置 (GUI 集成)
    const fcitx::Configuration *getConfig() const override {
        return &config_;
    }
    void setConfig(const fcitx::RawConfig &rawConfig) override {
        config_.load(rawConfig, true);
        applyConfig();
    }

    auto factory() const { return &factory_; }
    auto instance() const { return instance_; }

    // 便捷配置访问
    const std::string &backendSocket() const {
        return config_.backendSocket.value();
    }
    double vadSilenceTimeout() const {
        return config_.vadSilenceTimeoutMs.value() / 1000.0;
    }
    int asrIntermediateInterval() const {
        return config_.asrIntermediateInterval.value();
    }

    BackendClient &backend() { return *backend_; }
    YuHuangState *currentState();

    // PTT 状态
    bool isListening() const { return listening_; }
    void setListening(bool v) { listening_ = v; }

private:
    void applyConfig();
    void checkSystemConflict(const fcitx::Key &key);
    void sendConfigToBackend();

    fcitx::Instance *instance_;
    fcitx::FactoryFor<YuHuangState> factory_;
    YuHuangConfig config_;

    // PTT 触发键
    fcitx::Key triggerKey_;
    bool listening_ = false;
    bool triggerPressed_ = false;

    // 后端客户端
    std::unique_ptr<BackendClient> backend_;

    // 跨线程事件调度 (receiveLoop → fcitx5 主线程)
    fcitx::EventDispatcher eventDispatcher_;
};

// ===== Addon 工厂 =====
class YuHuangEngineFactory : public fcitx::AddonFactory {
    fcitx::AddonInstance *create(fcitx::AddonManager *manager) override {
        return new YuHuangEngine(manager->instance());
    }
};

} // namespace yuhuang

#endif // YUHUANG_ENGINE_H
