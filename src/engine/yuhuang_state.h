#ifndef YUHUANG_STATE_H
#define YUHUANG_STATE_H

#include "yuhuang_engine.h"
#include <fcitx/inputpanel.h>
#include <fcitx/text.h>

namespace yuhuang {

inline YuHuangState::YuHuangState(YuHuangEngine *engine,
                                   fcitx::InputContext *ic)
    : engine_(engine), ic_(ic) {}

inline YuHuangState::~YuHuangState() {}

inline void YuHuangState::updatePreedit(const std::string &text) {
    if (!ic_) return;

    auto &inputPanel = ic_->inputPanel();

    if (text.empty()) {
        inputPanel.reset();
        ic_->updateUserInterface(fcitx::UserInterfaceComponent::InputPanel);
        ic_->updatePreedit();
        return;
    }

    // 始终使用 fcitx5 自身渲染的 preedit（弹出候选窗），而非客户端内嵌 preedit
    fcitx::Text preedit(text);
    inputPanel.setPreedit(preedit);

    ic_->updateUserInterface(fcitx::UserInterfaceComponent::InputPanel);
    ic_->updatePreedit();
}

inline void YuHuangState::commitText(const std::string &text) {
    if (!ic_ || text.empty()) return;

    // 先清除 preedit，再提交文本（防止客户端混淆 preedit 和已提交文字）
    auto &inputPanel = ic_->inputPanel();
    inputPanel.reset();
    ic_->updateUserInterface(fcitx::UserInterfaceComponent::InputPanel);
    ic_->updatePreedit();

    ic_->commitString(text);
}

inline void YuHuangState::reset() {
    updatePreedit("");
}

} // namespace yuhuang

#endif // YUHUANG_STATE_H
