#include <iostream>
#include <string_view>

int main(int argc, char** argv) {
    if (argc == 2 && std::string_view(argv[1]) == "--version") {
        std::cout << "forge 0.1.0-dev\n";
        return 0;
    }
    if (argc == 1 || (argc == 2 && std::string_view(argv[1]) == "--help")) {
        std::cout << "Forge - private batch compute across your machines\n\n"
                     "Usage: forge --help | --version\n\n"
                     "Development foundation: remote execution commands are not yet implemented.\n";
        return 0;
    }
    std::cerr << "Unsupported command. Run forge --help.\n";
    return 2;
}
