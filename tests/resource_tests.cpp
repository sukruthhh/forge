#include "forge/resources.hpp"
#include <cstdlib>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <stdexcept>

namespace fs = std::filesystem;
void require(bool value, const char* message) { if (!value) throw std::runtime_error(message); }
void put(const fs::path& path, const std::string& value) { std::ofstream(path) << value; }
int main() {
    char pattern[] = "/tmp/forge-capacity-test-XXXXXX";
    const auto folder = ::mkdtemp(pattern);
    if (!folder) throw std::runtime_error("cannot create parser fixture");
    const fs::path root(folder), parent = root / "parent", leaf = parent / "leaf";
    fs::create_directories(leaf);
    try {
        put(root / "cpu.max", "200000 100000\n");
        put(parent / "cpu.max", "350000 100000\n");
        put(leaf / "cpu.max", "max 100000\n");
        put(parent / "memory.max", "500\n");
        put(parent / "memory.current", "120\n");
        put(leaf / "memory.max", "max\n");
        auto capacity = forge::constrain_capacity({8, 1000}, leaf, root);
        require(capacity.cpu == 2 && capacity.memory_bytes == 380, "ancestor CPU/memory limits ignored");
        capacity = forge::constrain_capacity({1, 200}, leaf, root);
        require(capacity.cpu == 1 && capacity.memory_bytes == 200, "affinity/physical memory constraint relaxed");
        put(leaf / "cpu.max", "150000 100000\n");
        require(forge::constrain_capacity({8, 1000}, leaf, root).cpu == 1, "fractional capacity rounded upward");
        put(leaf / "cpu.max", "50000 100000\n");
        require(forge::constrain_capacity({8, 1000}, leaf, root).cpu == 0, "sub-core quota overadvertised");
        put(leaf / "cpu.max", "max 100000\n");
        put(parent / "memory.current", "600\n");
        require(forge::constrain_capacity({8, 1000}, leaf, root).memory_bytes == 0, "memory headroom underflowed");
        for (const auto& value : {"100000 0", "bad 100000", "18446744073709551616 100000", "max 100000 extra"}) {
            put(leaf / "cpu.max", value);
            bool rejected = false;
            try { forge::constrain_capacity({8, 1000}, leaf, root); } catch (...) { rejected = true; }
            require(rejected, "invalid resource control accepted");
        }
        bool rejected = false;
        try { forge::CgroupRoot invalid(root.string()); } catch (...) { rejected = true; }
        require(rejected, "ordinary parser-fixture files accepted as real enforcement");
        rejected = false;
        try { forge::constrain_capacity({8, 1000}, root, leaf); } catch (...) { rejected = true; }
        require(rejected, "capacity scan escaped its boundary");
    } catch (...) { fs::remove_all(root); throw; }
    fs::remove_all(root);
    std::cout << "Resource parser checks passed; real enforcement requires the separate kernel fixture\n";
}
