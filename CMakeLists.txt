cmake_minimum_required(VERSION 3.21)

project(excido)

message("Building with CMake version: ${CMAKE_VERSION}")

set(CMAKE_C_COMPILER "clang")
set(CMAKE_CXX_COMPILER "clang++")
set(CMAKE_CXX_STANDARD 23)
set(CMAKE_CXX_STANDARD_REQUIRED True)
set(CMAKE_EXPORT_COMPILE_COMMANDS ON)

find_package(LLVM CONFIG REQUIRED)
find_package(Clang CONFIG REQUIRED)
message(STATUS "Found LLVM ${LLVM_PACKAGE_VERSION}")

list(APPEND CMAKE_MODULE_PATH "${LLVM_CMAKE_DIR}" "${CLANG_CMAKE_DIR}")

include(AddLLVM)
include(AddClang)
include(HandleLLVMOptions)

option(LINK_STATIC "Link against static LLVM/Clang libraries (larger binary, no runtime deps)" OFF)

set(LLVM_LINK_COMPONENTS support)

if(LINK_STATIC)
  set(FUNC_SCANNER_LIBS clangBasic clangFrontend clangLex clangTooling)
  set(EXCIDO_LIBS
    libclang clangAST clangASTMatchers clangBasic
    clangFrontend clangLex clangIndex clangSerialization clangTooling)
else()
  set(FUNC_SCANNER_LIBS clang-cpp LLVM)
  set(EXCIDO_LIBS clang-cpp LLVM)
endif()

add_clang_executable(excido cxx/fuzz_builder.cpp)
target_include_directories(excido PRIVATE ${LLVM_INCLUDE_DIRS})
target_link_libraries(excido PRIVATE ${EXCIDO_LIBS})

add_clang_executable(func_scanner cxx/func_scanner.cpp)
target_include_directories(func_scanner PRIVATE ${LLVM_INCLUDE_DIRS})
target_link_libraries(func_scanner PRIVATE ${FUNC_SCANNER_LIBS})
