# Builds obv-dump: the OpenBoardView file parsers without the GUI, plus a JSON writer.
# OBV_SOURCE_DIR is an OpenBoardView source tree with the mpc and utf8 submodules and the patches in patches/.
# flake.nix gives it. For a manual build:
#   cmake -S boardview -B build -DOBV_SOURCE_DIR=/path/to/OpenBoardView && cmake --build build
cmake_minimum_required(VERSION 3.20)
project(obv-dump VERSION 0.1.0 LANGUAGES C CXX)

set(OBV_SOURCE_DIR "" CACHE PATH "OpenBoardView source tree (patched, with src/mpc and src/utf8)")
set(OBV_VERSION "10.0.0" CACHE STRING "OpenBoardView version in OBV_SOURCE_DIR")
if(NOT OBV_SOURCE_DIR)
	message(FATAL_ERROR "Set OBV_SOURCE_DIR to an OpenBoardView source tree")
endif()

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

find_package(ZLIB REQUIRED)
find_package(nlohmann_json 3 REQUIRED)
find_package(Python REQUIRED COMPONENTS Interpreter)

set(OBV_SRC "${OBV_SOURCE_DIR}/src")
set(OBV_APP "${OBV_SRC}/openboardview")

# The GenCAD parser includes a grammar header that a script makes from the BNF file.
set(GENCAD_GRAMMAR_H "${CMAKE_CURRENT_BINARY_DIR}/generated/build-generated/GenCADFileGrammar.h")
add_custom_command(
	OUTPUT "${GENCAD_GRAMMAR_H}"
	COMMAND "${Python_EXECUTABLE}" "${OBV_SOURCE_DIR}/utilities/generate_grammar_header.py"
		"${OBV_APP}/FileFormats/GenCADFileBnf.h" "${GENCAD_GRAMMAR_H}"
	DEPENDS "${OBV_SOURCE_DIR}/utilities/generate_grammar_header.py" "${OBV_APP}/FileFormats/GenCADFileBnf.h"
)

add_executable(obv-dump
	src/main.cpp
	"${OBV_APP}/utils.cpp"
	"${OBV_APP}/Crypto/des.c"
	"${OBV_APP}/FileFormats/ADFile.cpp"
	"${OBV_APP}/FileFormats/ASCFile.cpp"
	"${OBV_APP}/FileFormats/BDVFile.cpp"
	"${OBV_APP}/FileFormats/BRD2File.cpp"
	"${OBV_APP}/FileFormats/BRDFile.cpp"
	"${OBV_APP}/FileFormats/BRDFileBase.cpp"
	"${OBV_APP}/FileFormats/BVR3File.cpp"
	"${OBV_APP}/FileFormats/BVRFile.cpp"
	"${OBV_APP}/FileFormats/CADFile.cpp"
	"${OBV_APP}/FileFormats/CAEFile.cpp"
	"${OBV_APP}/FileFormats/CSTFile.cpp"
	"${OBV_APP}/FileFormats/FZFile.cpp"
	"${OBV_APP}/FileFormats/GenCADFile.cpp"
	"${OBV_APP}/FileFormats/XZZPCBFile.cpp"
	"${OBV_SRC}/mpc/mpc.c"
	"${GENCAD_GRAMMAR_H}"
)

target_include_directories(obv-dump PRIVATE
	# The stub SDL.h comes first, so the parsers need no SDL.
	"${CMAKE_CURRENT_SOURCE_DIR}/include"
	"${CMAKE_CURRENT_BINARY_DIR}/generated"
	"${OBV_APP}"
	"${OBV_SRC}"
)
target_compile_definitions(obv-dump PRIVATE
	WITH_STD_FILESYSTEM
	OBV_VERSION="${OBV_VERSION}"
	OBV_DUMP_VERSION="${PROJECT_VERSION}"
)
target_link_libraries(obv-dump PRIVATE ZLIB::ZLIB nlohmann_json::nlohmann_json)

install(TARGETS obv-dump RUNTIME DESTINATION bin)
