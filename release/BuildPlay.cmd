@echo off
rem Run from an x64 Visual Studio developer command prompt.
rem Set QT_ROOT to Qt 5.15.2 msvc2019_64 and ZLIB_ROOT to a built shared zlib 1.3.2.
if not defined QT_ROOT exit /b 2
if not defined ZLIB_ROOT exit /b 2
cmake -S Play -B build -G Ninja -DCMAKE_BUILD_TYPE=Release -DBUILD_TESTS=OFF -DBUILD_PLAY=ON -DBUILD_PSFPLAYER=OFF -DDEBUGGER_INCLUDED=ON -DPLAY_DEPLOY_QT=OFF -DCMAKE_DISABLE_FIND_PACKAGE_Qt6=ON "-DCMAKE_PREFIX_PATH=%QT_ROOT%" "-DQt5_DIR=%QT_ROOT%/lib/cmake/Qt5" "-DWINDEPLOYQT_EXE=%QT_ROOT%/bin/windeployqt.exe" "-DZLIB_INCLUDE_DIR=%ZLIB_ROOT%/include" "-DZLIB_LIBRARY_RELEASE=%ZLIB_ROOT%/lib/z.lib" "-DVulkan_INCLUDE_DIR=%CD%/Vulkan-Headers/include" "-DVulkan_LIBRARY=%SystemRoot%/System32/vulkan-1.dll" || exit /b
cmake --build build --target Play --parallel
