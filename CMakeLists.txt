cmake_minimum_required(VERSION 3.21)
project(yuhuang VERSION 0.2.0 LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

# 查找 fcitx5 依赖
find_package(Fcitx5Core 5.1.0 REQUIRED)
find_package(Fcitx5Config QUIET)
find_package(PkgConfig REQUIRED)

# fcitx5 编译设置
include("${FCITX_INSTALL_CMAKECONFIG_DIR}/Fcitx5Utils/Fcitx5CompilerSettings.cmake")

add_subdirectory(src/engine)
