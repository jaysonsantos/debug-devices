// Stub for <SDL.h>. The OpenBoardView parsers use SDL only for log output.
// obv-dump writes these logs to stderr. Stdout carries the JSON.
#pragma once

#include <cstdio>

#define SDL_LOG_CATEGORY_APPLICATION 0
#define SDL_LOG_CATEGORY_ERROR 1

#define SDL_LogError(category, ...) (std::fprintf(stderr, __VA_ARGS__), std::fputc('\n', stderr))
#define SDL_LogWarn(category, ...) (std::fprintf(stderr, __VA_ARGS__), std::fputc('\n', stderr))
#define SDL_LogInfo(category, ...) (std::fprintf(stderr, __VA_ARGS__), std::fputc('\n', stderr))
#define SDL_LogDebug(category, ...) ((void)0)
