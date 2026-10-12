# home-manager module: the `verdict` CLI for agents on any machine, pointed at
# a Verdict server by VERDICT_URL. The server itself is the nix-darwin module.
self:
{ config, lib, pkgs, ... }:

let
  cfg = config.programs.verdict;
in
{
  options.programs.verdict = {
    enable = lib.mkEnableOption "the Verdict CLI";

    package = lib.mkOption {
      type = lib.types.package;
      default = self.packages.${pkgs.stdenv.hostPlatform.system}.default;
      description = "The Verdict package; only its `verdict` command goes on PATH.";
    };

    url = lib.mkOption {
      type = lib.types.str;
      description = ''
        Origin of the Verdict server, exported as VERDICT_URL. Review links
        are built from it, so use the name the reviewer's browser reaches.
      '';
      example = "http://host.example.ts.net:7071";
    };
  };

  config = lib.mkIf cfg.enable {
    # Only the command: the package's venv also ships python3, which would
    # shadow the user's own.
    home.packages = [
      (pkgs.runCommand "verdict-cli" { } ''
        mkdir -p $out/bin
        ln -s ${lib.getExe' cfg.package "verdict"} $out/bin/verdict
      '')
    ];
    home.sessionVariables.VERDICT_URL = cfg.url;
    # Session variables reach only shells that read them fresh. Shells that
    # inherit an older session's environment (terminal multiplexer panes, ssh
    # commands) skip them, and .zshenv runs in every zsh.
    programs.zsh.envExtra = lib.mkAfter "export VERDICT_URL=${lib.escapeShellArg cfg.url}\n";
  };
}
