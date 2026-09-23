{
  description = "debug-devices: phone camera app and MCP server that let a coding agent see real hardware";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

  outputs =
    { nixpkgs, ... }:
    let
      forAllSystems = nixpkgs.lib.genAttrs [
        "x86_64-linux"
        "aarch64-linux"
        "aarch64-darwin"
      ];

      # Android SDK versions. AGP 9.4 needs build-tools 36.0.0 or later and supports compileSdk up to 37.
      androidPlatformVersion = "36";
      androidBuildToolsVersion = "36.0.0";
    in
    {
      devShells = forAllSystems (
        system:
        let
          pkgs = import nixpkgs {
            inherit system;
            config = {
              # The Android SDK is unfree and has its own license.
              allowUnfree = true;
              android_sdk.accept_license = true;
            };
          };

          androidComposition = pkgs.androidenv.composeAndroidPackages {
            platformVersions = [ androidPlatformVersion ];
            buildToolsVersions = [ androidBuildToolsVersion ];
            includeEmulator = false;
            includeSystemImages = false;
            includeNDK = false;
          };
          androidSdk = androidComposition.androidsdk;
          androidHome = "${androidSdk}/libexec/android-sdk";
        in
        {
          default = pkgs.mkShell {
            packages = [
              # mcp/: Python MCP server and the webcam capture
              pkgs.python314
              pkgs.uv
              pkgs.ruff
              pkgs.ffmpeg

              # android/: Kotlin app. AGP 9.4 needs JDK 17 and Gradle 9.6 or later.
              pkgs.jdk17
              pkgs.gradle_9
              pkgs.android-tools
              pkgs.ktlint
              androidSdk

              # Linters for the whole repository, run by prek
              pkgs.prek
              pkgs.typos
              pkgs.taplo
              pkgs.nixfmt
              pkgs.shellcheck
            ]
            # v4l2-ctl lists the webcam formats. Video4Linux exists only on Linux.
            ++ pkgs.lib.optionals pkgs.stdenv.hostPlatform.isLinux [ pkgs.v4l-utils ];

            ANDROID_HOME = androidHome;
            ANDROID_SDK_ROOT = androidHome;
            JAVA_HOME = pkgs.jdk17.home;
            # The aapt2 that AGP downloads from Maven is dynamically linked and does not run on NixOS.
            GRADLE_OPTS = "-Dorg.gradle.project.android.aapt2FromMavenOverride=${androidHome}/build-tools/${androidBuildToolsVersion}/aapt2";
            # uv uses the Python from the flake (first on PATH), not a downloaded one.
            # Do not set UV_PYTHON: it makes `uv pip install` write into the read-only Nix store.
            UV_PYTHON_DOWNLOADS = "never";
            UV_PYTHON_PREFERENCE = "only-system";
          };
        }
      );
    };
}
