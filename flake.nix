# This is a Nix flake configuration file.
#
# To enter the development shell, run: nix develop
# Alternatively, if you use direnv, add 'use flake' to your .envrc to automatically activate the
# development environment when entering the project directory.

{
  description = "repoactive";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixpkgs-unstable";
  };

  outputs =
    { self, nixpkgs }:
    let
      supportedSystems = [
        "x86_64-linux"
        "aarch64-linux"
        "x86_64-darwin"
        "aarch64-darwin"
      ];
      forAllSystems = nixpkgs.lib.genAttrs supportedSystems;
    in
    {
      devShells = forAllSystems (
        system:
        let
          pkgs = nixpkgs.legacyPackages.${system};
        in
        {
          default = pkgs.mkShell {
            packages = with pkgs; [
              gitMinimal
              jujutsu
              just
              prettier
              pyright
              ruff
              shellcheck
              shfmt
              taplo
              treefmt
              uv
            ];
          };
        }
      );
      packages = forAllSystems (
        system:
        let
          pkgs = nixpkgs.legacyPackages.${system};
        in
        {
          # A single closure with just the dev tools (no stdenv / build
          # toolchain). Used to build a slim Docker image — see
          # Dockerfile.devtools.
          devtools = pkgs.buildEnv {
            name = "repoactive-devtools";
            paths = with pkgs; [
              git
              jujutsu
              just
              uv
            ];
          };
        }
      );
    };
}
