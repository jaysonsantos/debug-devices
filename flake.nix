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
      # nixpkgs names platform 37 "37.0" (directory platforms/android-37.0).
      androidPlatformVersion = "37.0";
      androidBuildToolsVersion = "37.0.0";

      pkgsFor =
        system:
        import nixpkgs {
          inherit system;
          config = {
            # The Android SDK is unfree and has its own license.
            allowUnfree = true;
            android_sdk.accept_license = true;
          };
        };

      # boardview/: obv-dump, the OpenBoardView file parsers without the GUI.
      # OpenBoardView stays pinned to a release tag. Our changes are patch files in boardview/patches/.
      obvVersion = "10.0.0";
      obvDumpFor =
        pkgs:
        let
          obvSource = pkgs.applyPatches {
            name = "openboardview-${obvVersion}-parsers";
            src = pkgs.fetchFromGitHub {
              owner = "OpenBoardView";
              repo = "OpenBoardView";
              tag = obvVersion;
              hash = "sha256-KJcMfJMfICDcM4GJiHSAIS00OKDZUTAiCroKIXl7S+I=";
            };
            patches = pkgs.lib.fileset.toList (
              pkgs.lib.fileset.fileFilter (f: f.hasExt "patch") ./boardview/patches
            );
            # The parsers need only two of the submodules. The revisions are the gitlinks of the tag.
            postPatch = ''
              rm -rf src/mpc src/utf8
              cp -r ${
                pkgs.fetchFromGitHub {
                  owner = "orangeduck";
                  repo = "mpc";
                  rev = "65f20a1a0b3249a475efa8ecb7b5ecd2c1c071c4";
                  hash = "sha256-7kJxk3Z0jgxQo45LalvsDMAikc6+uAzuW0BwYWWMOAE=";
                }
              } src/mpc
              cp -r ${
                pkgs.fetchFromGitHub {
                  owner = "sheredom";
                  repo = "utf8.h";
                  rev = "3e9e3ec15c7bf129664ab2a113eb03b54ee0b584";
                  hash = "sha256-2dfHd/C9u1idikOe5P8cl4dr7pPbVqW5+grDoNSJd94=";
                }
              } src/utf8
            '';
          };
        in
        pkgs.stdenv.mkDerivation {
          pname = "obv-dump";
          version = "0.1.0";
          src = pkgs.lib.fileset.toSource {
            root = ./boardview;
            fileset = pkgs.lib.fileset.unions [
              ./boardview/CMakeLists.txt
              ./boardview/include
              ./boardview/src
            ];
          };
          nativeBuildInputs = [
            pkgs.cmake
            pkgs.python3
          ];
          buildInputs = [
            pkgs.zlib
            pkgs.nlohmann_json
          ];
          cmakeFlags = [
            "-DOBV_SOURCE_DIR=${obvSource}"
            "-DOBV_VERSION=${obvVersion}"
          ];
          # The MIT and BSD licenses of the parser code ask for their notices next to the binary.
          postInstall = ''
            install -Dm644 ${obvSource}/LICENSE $out/share/licenses/obv-dump/OpenBoardView.txt
            install -Dm644 ${obvSource}/src/mpc/LICENSE.md $out/share/licenses/obv-dump/mpc.txt
            install -Dm644 ${obvSource}/src/utf8/LICENSE $out/share/licenses/obv-dump/utf8.txt
          '';
          meta = {
            description = "Dump boardview files as JSON with the OpenBoardView parsers";
            license = pkgs.lib.licenses.mit;
            mainProgram = "obv-dump";
          };
        };
    in
    {
      packages = forAllSystems (system: {
        obv-dump = obvDumpFor (pkgsFor system);
      });

      devShells = forAllSystems (
        system:
        let
          pkgs = pkgsFor system;

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

              # scripts/: live reload (dev-monitor.sh) and the MCP config JSON (agent.sh)
              pkgs.watchexec
              pkgs.jq

              # android/: Kotlin app. AGP 9.4 needs JDK 17 and Gradle 9.6 or later.
              pkgs.jdk17
              pkgs.gradle_9
              pkgs.android-tools
              pkgs.ktlint
              androidSdk

              # boardview/: the boardview file parser CLI that the MCP server runs
              (obvDumpFor pkgs)

              # Linters for the whole repository, run by prek
              pkgs.prek
              pkgs.typos
              pkgs.taplo
              pkgs.nixfmt
              pkgs.shellcheck
            ]
            # Linux only: v4l2-ctl lists the webcam formats (Video4Linux),
            # and scrcpy mirrors the phone screen for the monitor feature.
            ++ pkgs.lib.optionals pkgs.stdenv.hostPlatform.isLinux [
              pkgs.v4l-utils
              pkgs.scrcpy
            ];

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
