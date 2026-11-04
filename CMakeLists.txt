cmake_minimum_required(VERSION 3.20)
project(Forge VERSION 0.1.0 LANGUAGES CXX)

add_library(forge_core src/scheduler.cpp)
target_include_directories(forge_core PUBLIC include)
target_compile_features(forge_core PUBLIC cxx_std_20)
set_target_properties(forge_core PROPERTIES CXX_EXTENSIONS OFF)
if(CMAKE_CXX_COMPILER_ID MATCHES "GNU|Clang")
  target_compile_options(forge_core PRIVATE -Wall -Wextra -Wpedantic -Werror)
endif()

if(NOT CMAKE_SYSTEM_NAME STREQUAL "Linux")
  message(FATAL_ERROR "The network/runtime milestone requires Linux. Use Dockerfile.dev on Windows.")
endif()
find_package(Threads REQUIRED)
find_package(OpenSSL 3.0 REQUIRED)
include(CheckCXXSymbolExists)
check_cxx_symbol_exists(posix_spawn_file_actions_addchdir_np "spawn.h" FORGE_HAS_SPAWN_CHDIR)
if(NOT FORGE_HAS_SPAWN_CHDIR)
  message(FATAL_ERROR "Forge staging requires a Linux libc with posix_spawn_file_actions_addchdir_np.")
endif()
add_library(forge_network src/network.cpp src/runtime.cpp src/executor.cpp src/workload.cpp src/batch.cpp)
target_link_libraries(forge_network PUBLIC forge_core Threads::Threads OpenSSL::SSL)
target_compile_options(forge_network PRIVATE -Wall -Wextra -Wpedantic -Werror)
add_executable(forge src/cli.cpp)
target_link_libraries(forge PRIVATE forge_network)

include(CTest)
if(BUILD_TESTING)
  add_executable(scheduler_tests tests/scheduler_tests.cpp)
  target_link_libraries(scheduler_tests PRIVATE forge_core)
  add_test(NAME scheduler COMMAND scheduler_tests)
  add_executable(protocol_tests tests/protocol_tests.cpp)
  target_link_libraries(protocol_tests PRIVATE forge_network)
  add_test(NAME protocol COMMAND protocol_tests)
  find_package(Python3 REQUIRED COMPONENTS Interpreter)
  add_test(NAME network_integration COMMAND ${Python3_EXECUTABLE}
    ${CMAKE_CURRENT_SOURCE_DIR}/tests/network_integration.py $<TARGET_FILE:forge>)
  set_tests_properties(network_integration PROPERTIES TIMEOUT 45)
  add_test(NAME execution_integration COMMAND ${Python3_EXECUTABLE}
    ${CMAKE_CURRENT_SOURCE_DIR}/tests/execution_integration.py $<TARGET_FILE:forge>)
  set_tests_properties(execution_integration PROPERTIES TIMEOUT 90)
  add_test(NAME history_integration COMMAND ${Python3_EXECUTABLE}
    ${CMAKE_CURRENT_SOURCE_DIR}/tests/history_integration.py $<TARGET_FILE:forge>)
  set_tests_properties(history_integration PROPERTIES TIMEOUT 45)
  add_test(NAME dispatch_integration COMMAND ${Python3_EXECUTABLE}
    ${CMAKE_CURRENT_SOURCE_DIR}/tests/dispatch_integration.py $<TARGET_FILE:forge>)
  set_tests_properties(dispatch_integration PROPERTIES TIMEOUT 45)
  add_test(NAME authentication_integration COMMAND ${Python3_EXECUTABLE}
    ${CMAKE_CURRENT_SOURCE_DIR}/tests/authentication_integration.py $<TARGET_FILE:forge>)
  set_tests_properties(authentication_integration PROPERTIES TIMEOUT 45)
  add_test(NAME tls_integration COMMAND ${Python3_EXECUTABLE}
    ${CMAKE_CURRENT_SOURCE_DIR}/tests/tls_integration.py $<TARGET_FILE:forge>)
  set_tests_properties(tls_integration PROPERTIES TIMEOUT 90)
  add_test(NAME architecture_integration COMMAND ${Python3_EXECUTABLE}
    ${CMAKE_CURRENT_SOURCE_DIR}/tests/architecture_integration.py $<TARGET_FILE:forge>)
  set_tests_properties(architecture_integration PROPERTIES TIMEOUT 45)
  add_test(NAME staging_integration COMMAND ${Python3_EXECUTABLE}
    ${CMAKE_CURRENT_SOURCE_DIR}/tests/staging_integration.py $<TARGET_FILE:forge>)
  set_tests_properties(staging_integration PROPERTIES TIMEOUT 150)
  add_test(NAME benchmark_integration COMMAND ${Python3_EXECUTABLE}
    ${CMAKE_CURRENT_SOURCE_DIR}/tests/benchmark_integration.py $<TARGET_FILE:forge>)
  set_tests_properties(benchmark_integration PROPERTIES TIMEOUT 120)
endif()
