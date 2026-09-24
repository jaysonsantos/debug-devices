// obv-dump: read a boardview file with the OpenBoardView parsers and write JSON to stdout.
// The output follows docs/boardview-json.md.

#include <array>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <iostream>
#include <map>
#include <memory>
#include <optional>
#include <set>
#include <sstream>
#include <string>
#include <string_view>
#include <tuple>
#include <vector>

#include <nlohmann/json.hpp>

#include "FileFormats/ADFile.h"
#include "FileFormats/ASCFile.h"
#include "FileFormats/BDVFile.h"
#include "FileFormats/BRD2File.h"
#include "FileFormats/BRDAllegroFile.h"
#include "FileFormats/BRDFile.h"
#include "FileFormats/BRDFileBase.h"
#include "FileFormats/BVR3File.h"
#include "FileFormats/BVRFile.h"
#include "FileFormats/CADFile.h"
#include "FileFormats/CAEFile.h"
#include "FileFormats/CSTFile.h"
#include "FileFormats/FZFile.h"
#include "FileFormats/GenCADFile.h"
#include "FileFormats/XZZPCBFile.h"
#include "utils.h"

#ifndef OBV_DUMP_VERSION
#error "OBV_DUMP_VERSION must be defined by the build"
#endif
#ifndef OBV_VERSION
#error "OBV_VERSION must be defined by the build"
#endif

using json = nlohmann::ordered_json;

// region: constants

namespace contract {
constexpr int kSchemaVersion   = 1;
constexpr const char *kUnitMil = "mil";
} // namespace contract

namespace exit_code {
constexpr int kOk     = 0;
constexpr int kFailed = 1;
constexpr int kUsage  = 2;
} // namespace exit_code

namespace flag {
constexpr std::string_view kFzKey   = "--fz-key";
constexpr std::string_view kCaeKey  = "--cae-key";
constexpr std::string_view kXzzKey  = "--xzz-key";
constexpr std::string_view kVersion = "--version";
constexpr std::string_view kHelp    = "--help";
} // namespace flag

namespace ext {
constexpr const char *kFz  = ".fz";
constexpr const char *kCae = ".cae";
constexpr const char *kAsc = ".asc";
constexpr const char *kBom = ".bom";
constexpr const char *kCst = ".cst";
} // namespace ext

namespace error_code {
constexpr const char *kUnknownFormat = "unknown_format";
constexpr const char *kParseFailed   = "parse_failed";
constexpr const char *kKeyRequired   = "key_required";
constexpr const char *kKeyInvalid    = "key_invalid";
constexpr const char *kIoError       = "io_error";
} // namespace error_code

// OpenBoardView names the parts that only hold test pads "..." (BRDBoard::kComponentDummyName).
constexpr std::string_view kDummyPartName = "...";
// An FZ or CAE key has 44 words of 32 bits (FZFile::parse).
constexpr size_t kFzKeyWords = 44;
constexpr int kHexBase       = 16;
// A repeated part name gets this separator and a counter ("REF**#2"), so that part names are unique in one dump.
constexpr const char *kDuplicateNameSeparator = "#";
constexpr size_t kFirstDuplicateNumber        = 2;

constexpr const char *kUsageText =
    "usage: obv-dump <file> [--fz-key <hex>] [--cae-key <hex>] [--xzz-key <hex>]\n"
    "       obv-dump --version\n"
    "\n"
    "--fz-key, --cae-key: 44 hex words (0x prefix optional), separated by commas or spaces.\n"
    "--xzz-key: one 64-bit hex value.\n";

// endregion: constants

// region: formats

enum class Format { Brd, Brd2, Bdv, Asc, Bvr, Bvr3, Cad, Cst, Fz, Cae, GenCad, Ad, Xzz, Allegro };

// The one place that maps a format to its contract name.
const char *format_name(Format format) {
	switch (format) {
		case Format::Brd: return "brd";
		case Format::Brd2: return "brd2";
		case Format::Bdv: return "bdv";
		case Format::Asc: return "asc";
		case Format::Bvr: return "bvr";
		case Format::Bvr3: return "bvr3";
		case Format::Cad: return "cad";
		case Format::Cst: return "cst";
		case Format::Fz: return "fz";
		case Format::Cae: return "cae";
		case Format::GenCad: return "gencad";
		case Format::Ad: return "ad";
		case Format::Xzz: return "xzz";
		case Format::Allegro: return "allegro";
	}
	return "";
}

bool needs_key(Format format) {
	return format == Format::Fz || format == Format::Cae || format == Format::Xzz;
}

// Same order as BoardView::LoadFile in OpenBoardView: some formats by extension, the rest by content.
std::optional<Format> detect_format(const filesystem::path &path, std::vector<char> &buf) {
	if (check_fileext(path, ext::kFz)) return Format::Fz;
	if (check_fileext(path, ext::kCae)) return Format::Cae;
	if (check_fileext(path, ext::kBom) || check_fileext(path, ext::kAsc)) return Format::Asc;
	if (GenCADFile::verifyFormat(buf)) return Format::GenCad;
	if (ADFile::verifyFormat(buf)) return Format::Ad;
	if (CADFile::verifyFormat(buf)) return Format::Cad;
	if (check_fileext(path, ext::kCst)) return Format::Cst;
	if (BRDFile::verifyFormat(buf)) return Format::Brd;
	if (BRD2File::verifyFormat(buf)) return Format::Brd2;
	if (BDVFile::verifyFormat(buf)) return Format::Bdv;
	if (BVRFile::verifyFormat(buf)) return Format::Bvr;
	if (BVR3File::verifyFormat(buf)) return Format::Bvr3;
	if (BRDAllegroFile::verifyFormat(buf)) return Format::Allegro;
	if (XZZPCBFile::verifyFormat(buf)) return Format::Xzz;
	return std::nullopt;
}

// endregion: formats

// region: keys

struct Keys {
	std::optional<std::array<uint32_t, kFzKeyWords>> fz;
	std::optional<std::array<uint32_t, kFzKeyWords>> cae;
	std::optional<uint64_t> xzz;
};

std::optional<uint64_t> parse_hex(const std::string &word) {
	if (word.empty()) return std::nullopt;
	char *end            = nullptr;
	unsigned long long v = std::strtoull(word.c_str(), &end, kHexBase);
	if (end == word.c_str() || *end != '\0') return std::nullopt;
	return static_cast<uint64_t>(v);
}

std::optional<std::array<uint32_t, kFzKeyWords>> parse_fz_key(std::string text) {
	for (char &c : text)
		if (c == ',') c = ' ';
	std::istringstream words(text);
	std::array<uint32_t, kFzKeyWords> key{};
	size_t count = 0;
	std::string word;
	while (words >> word) {
		auto value = parse_hex(word);
		if (!value || *value > UINT32_MAX || count >= kFzKeyWords) return std::nullopt;
		key[count++] = static_cast<uint32_t>(*value);
	}
	if (count != kFzKeyWords) return std::nullopt;
	return key;
}

// endregion: keys

// region: output

// Some files have names that are not valid UTF-8. Replace the bad bytes, so that the output is always valid JSON.
std::string dump_text(const json &value) {
	constexpr int kNoIndent = -1;
	return value.dump(kNoIndent, ' ', false, json::error_handler_t::replace);
}

int write_error(const char *code, const std::string &message, std::optional<Format> format) {
	json out;
	out["schema_version"] = contract::kSchemaVersion;
	out["error"]          = code;
	out["message"]        = message;
	out["format"]         = format ? json(format_name(*format)) : json(nullptr);
	std::cout << dump_text(out) << '\n';
	return exit_code::kFailed;
}

const char *side_name(BRDPartMountingSide side) {
	switch (side) {
		case BRDPartMountingSide::Top: return "top";
		case BRDPartMountingSide::Bottom: return "bottom";
		case BRDPartMountingSide::Both: return "both";
	}
	return "both";
}

const char *side_name(BRDPinSide side) {
	switch (side) {
		case BRDPinSide::Top: return "top";
		case BRDPinSide::Bottom: return "bottom";
		case BRDPinSide::Both: return "both";
	}
	return "both";
}

const char *mounting_name(BRDPartType type) {
	return type == BRDPartType::ThroughHole ? "through_hole" : "smd";
}

json point(const BRDPoint &p) {
	return {{"x", p.x}, {"y", p.y}};
}

json optional_point(const BRDPoint &p, bool present) {
	return present ? point(p) : json(nullptr);
}

std::string text(const char *s) {
	return s ? std::string(s) : std::string();
}

bool is_dummy(const BRDPart &part) {
	return part.name && std::string_view(part.name).substr(0, kDummyPartName.size()) == kDummyPartName;
}

json make_dump(const BRDFileBase &file, const std::string &path, Format format) {
	json out;
	out["schema_version"] = contract::kSchemaVersion;
	out["source"]         = {{"path", path}, {"format", format_name(format)}, {"obv_version", OBV_VERSION}};
	out["units"]          = contract::kUnitMil;

	json outline = json::array();
	for (const auto &p : file.format) outline.push_back(point(p));
	out["outline"] = outline;

	json segments = json::array();
	for (const auto &[a, b] : file.outline_segments) segments.push_back({{"a", point(a)}, {"b", point(b)}});
	out["outline_segments"] = segments;

	// Unique output name for each part.
	std::vector<std::string> part_names(file.parts.size());
	{
		std::set<std::string> used;
		for (const auto &part : file.parts)
			if (!is_dummy(part)) used.insert(text(part.name));
		std::map<std::string, size_t> seen;
		for (size_t i = 0; i < file.parts.size(); ++i) {
			std::string name = text(file.parts[i].name);
			if (is_dummy(file.parts[i]) || seen[name]++ == 0) {
				part_names[i] = name;
				continue;
			}
			std::string unique;
			for (size_t n = kFirstDuplicateNumber;; ++n) {
				unique = name + kDuplicateNameSeparator + std::to_string(n);
				if (used.insert(unique).second) break;
			}
			part_names[i] = unique;
		}
	}

	// BRDPin::part is a 1-based index into parts.
	auto part_of = [&](unsigned int index) -> const BRDPart * {
		return (index >= 1 && index <= file.parts.size()) ? &file.parts[index - 1] : nullptr;
	};

	// Count pins per part, and find the first pin of each part in the output order.
	std::vector<size_t> pin_count(file.parts.size(), 0);
	std::vector<std::optional<size_t>> first_pin(file.parts.size());
	json pins  = json::array();
	json nails = json::array();
	std::set<std::tuple<int, int, std::string, std::string>> nail_seen;

	auto add_nail = [&](const std::string &net, const BRDPoint &pos, const char *side, unsigned int probe) {
		if (!nail_seen.insert({pos.x, pos.y, std::string(side), net}).second) return;
		nails.push_back({{"net", net}, {"x", pos.x}, {"y", pos.y}, {"side", side}, {"probe", probe}});
	};

	for (const auto &pin : file.pins) {
		const BRDPart *part = part_of(pin.part);
		if (!part) continue;
		// Pins of "..." parts are test pads. They go to nails, so that part names stay unique.
		if (is_dummy(*part)) {
			add_nail(text(pin.net), pin.pos, side_name(pin.side), static_cast<unsigned int>(pin.probe));
			continue;
		}
		size_t index = pin.part - 1;
		if (!first_pin[index]) first_pin[index] = pins.size();
		++pin_count[index];
		pins.push_back({{"part", part_names[index]},
		                {"number", text(pin.snum)},
		                {"name", text(pin.name)},
		                {"net", text(pin.net)},
		                {"x", pin.pos.x},
		                {"y", pin.pos.y},
		                {"side", side_name(pin.side)},
		                {"radius", pin.radius},
		                {"probe", pin.probe}});
	}
	for (const auto &nail : file.nails) add_nail(text(nail.net), nail.pos, side_name(nail.side), nail.probe);

	json parts = json::array();
	for (size_t i = 0; i < file.parts.size(); ++i) {
		const BRDPart &part = file.parts[i];
		if (is_dummy(part)) continue;
		// The parsers leave p1 and p2 at 0,0 when the format has no part box.
		bool has_box = part.p1.x != 0 || part.p1.y != 0 || part.p2.x != 0 || part.p2.y != 0;
		parts.push_back({{"name", part_names[i]},
		                 {"side", side_name(part.mounting_side)},
		                 {"mounting", mounting_name(part.part_type)},
		                 {"p1", optional_point(part.p1, has_box)},
		                 {"p2", optional_point(part.p2, has_box)},
		                 {"rotation_deg", part.has_rotation ? json(part.rotation_deg) : json(nullptr)},
		                 {"mfgcode", part.mfgcode},
		                 {"first_pin", first_pin[i] ? json(*first_pin[i]) : json(nullptr)},
		                 {"pin_count", pin_count[i]}});
	}

	out["parts"] = parts;
	out["pins"]  = pins;
	out["nails"] = nails;
	return out;
}

// endregion: output

// region: parse

std::unique_ptr<BRDFileBase> parse(Format format, std::vector<char> &buf, const filesystem::path &path, const Keys &keys) {
	switch (format) {
		case Format::Fz: {
			auto file = std::make_unique<FZFile>();
			file->parse(buf, *keys.fz);
			return file;
		}
		case Format::Cae: {
			auto file = std::make_unique<CAEFile>();
			file->parse(buf, *keys.cae);
			return file;
		}
		case Format::Asc: return std::make_unique<ASCFile>(buf, path);
		case Format::GenCad: return std::make_unique<GenCADFile>(buf);
		case Format::Ad: return std::make_unique<ADFile>(buf);
		case Format::Cad: return std::make_unique<CADFile>(buf);
		case Format::Cst: return std::make_unique<CSTFile>(buf);
		case Format::Brd: return std::make_unique<BRDFile>(buf);
		case Format::Brd2: return std::make_unique<BRD2File>(buf);
		case Format::Bdv: return std::make_unique<BDVFile>(buf);
		case Format::Bvr: return std::make_unique<BVRFile>(buf);
		case Format::Bvr3: return std::make_unique<BVR3File>(buf);
		case Format::Xzz: return std::make_unique<XZZPCBFile>(buf, *keys.xzz);
		case Format::Allegro: return std::make_unique<BRDAllegroFile>(buf);
	}
	return nullptr;
}

bool has_key(Format format, const Keys &keys) {
	switch (format) {
		case Format::Fz: return keys.fz.has_value();
		case Format::Cae: return keys.cae.has_value();
		case Format::Xzz: return keys.xzz.has_value();
		default: return true;
	}
}

// The parsers report a bad key with a message that starts like this. The message also has the key, so it is not
// copied to the output.
bool is_key_error(const std::string &message) {
	return message.rfind("Invalid", 0) == 0 && message.find("ey") != std::string::npos;
}

// endregion: parse

int main(int argc, char **argv) {
	std::optional<std::string> input;
	Keys keys;

	for (int i = 1; i < argc; ++i) {
		std::string_view arg = argv[i];
		auto value           = [&]() -> std::optional<std::string> {
            if (i + 1 >= argc) return std::nullopt;
            return std::string(argv[++i]);
		};
		if (arg == flag::kVersion) {
			std::cout << "obv-dump " << OBV_DUMP_VERSION << " (OpenBoardView " << OBV_VERSION << ")\n";
			return exit_code::kOk;
		} else if (arg == flag::kHelp) {
			std::cout << kUsageText;
			return exit_code::kOk;
		} else if (arg == flag::kFzKey || arg == flag::kCaeKey) {
			auto text = value();
			auto key  = text ? parse_fz_key(*text) : std::nullopt;
			if (!key) {
				std::cerr << "obv-dump: " << arg << " needs " << kFzKeyWords << " hex words\n";
				return exit_code::kUsage;
			}
			(arg == flag::kFzKey ? keys.fz : keys.cae) = key;
		} else if (arg == flag::kXzzKey) {
			auto text = value();
			keys.xzz  = text ? parse_hex(*text) : std::nullopt;
			if (!keys.xzz) {
				std::cerr << "obv-dump: " << arg << " needs one 64-bit hex value\n";
				return exit_code::kUsage;
			}
		} else if (!arg.empty() && arg.front() == '-') {
			std::cerr << "obv-dump: unknown option " << arg << "\n" << kUsageText;
			return exit_code::kUsage;
		} else if (input) {
			std::cerr << "obv-dump: only one file\n" << kUsageText;
			return exit_code::kUsage;
		} else {
			input = std::string(arg);
		}
	}
	if (!input) {
		std::cerr << kUsageText;
		return exit_code::kUsage;
	}

	filesystem::path path = *input;
	std::error_code ec;
	auto absolute = filesystem::absolute(path, ec);
	std::string path_text = ec ? path.string() : absolute.string();

	std::string io_message;
	std::vector<char> buf = file_as_buffer(path, io_message);
	if (!io_message.empty()) return write_error(error_code::kIoError, io_message, std::nullopt);

	auto format = detect_format(path, buf);
	if (!format) return write_error(error_code::kUnknownFormat, "Unrecognized file format", std::nullopt);
	if (*format == Format::Allegro)
		// "allegro" is not a format name in the contract, so the error has no format.
		return write_error(error_code::kUnknownFormat, "Allegro binary .brd files are not supported", std::nullopt);
	if (needs_key(*format) && !has_key(*format, keys))
		return write_error(error_code::kKeyRequired, std::string("This format needs a key: --") + format_name(*format) + "-key",
		                   format);

	auto file = parse(*format, buf, path, keys);
	if (!file || !file->valid) {
		std::string message = file ? file->error_msg : std::string("no parser");
		if (needs_key(*format) && is_key_error(message))
			return write_error(error_code::kKeyInvalid, "The key is not valid for this file", format);
		return write_error(error_code::kParseFailed, message, format);
	}

	std::cout << dump_text(make_dump(*file, path_text, *format)) << '\n';
	return exit_code::kOk;
}
