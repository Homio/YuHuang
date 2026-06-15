# 生成 yuhuang.so (不带 lib 前缀)
add_library(yuhuang SHARED
    yuhuang_engine.cpp
)

target_link_libraries(yuhuang PRIVATE
    Fcitx5::Core
    Fcitx5::Config
    Fcitx5::Utils
)

target_include_directories(yuhuang PRIVATE
    ${CMAKE_CURRENT_SOURCE_DIR}
)

set_target_properties(yuhuang PROPERTIES PREFIX "")

# 安装共享库
install(TARGETS yuhuang DESTINATION "${FCITX_INSTALL_LIBDIR}/fcitx5")

# 安装 addon 注册文件
configure_file(yuhuang-addon.conf.in yuhuang-addon.conf)
install(FILES "${CMAKE_CURRENT_BINARY_DIR}/yuhuang-addon.conf"
        RENAME yuhuang.conf
        DESTINATION "${FCITX_INSTALL_PKGDATADIR}/addon")

# 安装输入法注册文件 (文件名必须与 addon 名称一致)
install(FILES yuhuang-inputmethod.conf
        RENAME yuhuang.conf
        DESTINATION "${FCITX_INSTALL_PKGDATADIR}/inputmethod")

# 安装配置文件 (带 GUI 描述，供 fcitx5-configtool 使用)
install(FILES yuhuang.conf
        DESTINATION "${FCITX_INSTALL_PKGDATADIR}/config")
