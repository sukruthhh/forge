#include "forge/runtime.hpp"
#include <charconv>
#include <csignal>
#include <iostream>
#include <stdexcept>
#include <string>
#include <string_view>

int main(int argc, char** argv) {
  std::signal(SIGPIPE, SIG_IGN);
  try {
    if (argc == 2 && std::string_view(argv[1]) == "--version") {
        std::cout << "forge 0.1.0-dev\n";
        return 0;
    }
    if (argc == 1 || (argc == 2 && std::string_view(argv[1]) == "--help")) {
        std::cout << "Forge - private batch compute across your machines\n\n"
                     "Usage:\n  forge coordinator [--port 9000]\n"
                     "  forge join --id NAME [--port 9000]\n"
                     "  forge cluster [--port 9000]\n"
                     "  forge ps [--port 9000]\n"
                     "  forge logs JOB [--port 9000]\n\n"
                     "  forge timings JOB [--port 9000]\n\n"
                     "  forge attempts JOB [--port 9000]\n\n"
                     "  forge run [--port 9000] [--cpu 1] [--memory 0] [--timeout 30] [--arch native|any|ARCH] -- COMMAND [ARGS...]\n\n"
                     "  forge run [OPTIONS] [--file PATH]... -- COMMAND [ARGS...]\n"
                     "  forge map [OPTIONS] [--file SHARED]... [--jobs 16] [--output DIR] INPUT... -- COMMAND {}\n\n"
                     "Staging: at most 16 flat files and 32768 data bytes per task; no dependency installation.\n"
                     "Map: at most 256 inputs; output directory must be new; jobs is a parallelism cap.\n"
                     "Run/map --retries 0..3: retry worker loss only (default 0); commands can execute more than once.\n"
                     "Network options: coordinator --bind IPv4; other commands --host IPv4 (default 127.0.0.1).\n"
                     "Remote endpoints require FORGE_TLS_CA, FORGE_TLS_CERT, FORGE_TLS_KEY (mutual TLS).\n"
                     "Architecture defaults to native. Use any only when the command and dependencies support every eligible worker.\n"
                     "Trusted commands, no sandbox. Optional additional authentication: FORGE_TOKEN.\n";
        return 0;
    }
    const std::string_view command(argv[1]);
    std::uint16_t port = 9000;
    std::string id, log_id;
    std::string endpoint = "127.0.0.1";
    std::string architecture = "native";
    bool architecture_seen = false;
    bool endpoint_seen = false;
    std::uint32_t cpu = 1, timeout = 30, retries = 0;
    std::uint64_t memory = 0;
    bool cpu_seen = false, memory_seen = false, timeout_seen = false, retries_seen = false;
    std::vector<std::string> arguments, files, inputs;
    std::size_t concurrency = 16;
    std::string output = "forge-map-results";
    bool concurrency_seen = false, output_seen = false;
    bool port_seen = false;
    int first_option = 2;
    if (command == "logs" || command == "timings" || command == "attempts") {
        if (argc < 3) throw std::runtime_error("inspection requires a job ID");
        log_id = argv[2];
        first_option = 3;
    }
    for (int i = first_option; i < argc; ++i) {
        if ((command == "run" || command == "map") && std::string_view(argv[i]) == "--") {
            for (++i; i < argc; ++i) arguments.emplace_back(argv[i]);
            break;
        }
        if (command == "map" && !std::string_view(argv[i]).starts_with("--")) {
            inputs.emplace_back(argv[i]);
            continue;
        }
        if (i + 1 >= argc) throw std::runtime_error("option needs a value");
        const std::string_view option(argv[i]);
        const std::string_view value(argv[++i]);
        if (option == "--port" && !port_seen) {
            unsigned parsed = 0;
            const auto [end, error] = std::from_chars(value.data(), value.data() + value.size(), parsed);
            if (error != std::errc{} || end != value.data() + value.size() || parsed == 0 || parsed > 65535)
                throw std::runtime_error("port must be between 1 and 65535");
            port = static_cast<std::uint16_t>(parsed);
            port_seen = true;
        } else if (!endpoint_seen && !value.empty() &&
                   ((option == "--bind" && command == "coordinator") ||
                    (option == "--host" && (command == "join" || command == "cluster" ||
                        command == "ps" || command == "logs" || command == "timings" || command == "attempts" ||
                        command == "run" || command == "map")))) {
            endpoint = value;
            endpoint_seen = true;
        } else if (option == "--arch" && (command == "run" || command == "map") && !architecture_seen) {
            architecture = value;
            architecture_seen = true;
        } else if (option == "--file" && (command == "run" || command == "map")) {
            files.emplace_back(value);
        } else if (option == "--output" && command == "map" && !output_seen && !value.empty()) {
            output = value;
            output_seen = true;
        } else if (option == "--jobs" && command == "map" && !concurrency_seen) {
            unsigned parsed = 0;
            const auto [end, error] = std::from_chars(value.data(), value.data() + value.size(), parsed);
            if (error != std::errc{} || end != value.data() + value.size() || parsed == 0 || parsed > 16)
                throw std::runtime_error("map jobs must be between 1 and 16");
            concurrency = parsed;
            concurrency_seen = true;
        } else if (option == "--id" && command == "join" && id.empty()) {
            id = value;
        } else if ((command == "run" || command == "map") &&
                   (option == "--cpu" || option == "--memory" || option == "--timeout" || option == "--retries")) {
            std::uint64_t parsed = 0;
            const auto [end, error] = std::from_chars(value.data(), value.data() + value.size(), parsed);
            if (error != std::errc{} || end != value.data() + value.size())
                throw std::runtime_error("invalid numeric job option");
            if (option == "--cpu" && !cpu_seen && parsed > 0 && parsed <= 1048576) {
                cpu = static_cast<std::uint32_t>(parsed); cpu_seen = true;
            } else if (option == "--memory" && !memory_seen) {
                memory = parsed; memory_seen = true;
            } else if (option == "--timeout" && !timeout_seen && parsed > 0 && parsed <= 30) {
                timeout = static_cast<std::uint32_t>(parsed); timeout_seen = true;
            } else if (option == "--retries" && !retries_seen && parsed <= 3) {
                retries = static_cast<std::uint32_t>(parsed); retries_seen = true;
            } else throw std::runtime_error("repeated or out-of-range job option");
        } else throw std::runtime_error("unknown or repeated option");
    }
    if (command == "coordinator") return forge::coordinator(port, endpoint);
    if (command == "cluster") return forge::cluster(port, endpoint);
    if (command == "ps") return forge::ps(port, endpoint);
    if (command == "logs") return forge::logs(port, log_id, endpoint);
    if (command == "timings") return forge::timings(port, log_id, endpoint);
    if (command == "attempts") return forge::attempts(port, log_id, endpoint);
    if (command == "join" && !id.empty()) return forge::join(port, id, endpoint);
    if ((command == "run" || command == "map") && !arguments.empty()) {
        forge::TaskRequest task{cpu, memory, timeout, architecture, arguments, forge::read_inputs(files), retries};
        if (command == "run") return forge::run(port, endpoint, task);
        return forge::map(port, endpoint, task, inputs, concurrency, output);
    }
    throw std::runtime_error("unsupported command; run forge --help");
  } catch (const std::exception& error) {
    std::cerr << "forge: " << error.what() << '\n';
    return 1;
  }
}
