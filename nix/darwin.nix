# nix-darwin module: the Verdict server as a kept-alive launchd user agent
# (`verdict serve`), plus the `verdict` CLI on PATH for the agents that run
# on this machine. Options are generic; the consumer supplies the bind
# address (a private interface, e.g. the host's tailnet IP), the port and,
# optionally, the URL clients should use.
self:
{ config, lib, pkgs, ... }:

let
  cfg = config.services.verdict;
  exe = lib.getExe' cfg.package "verdict";
in
{
  options.services.verdict = {
    enable = lib.mkEnableOption "the Verdict review server";

    package = lib.mkOption {
      type = lib.types.package;
      default = self.packages.${pkgs.stdenv.hostPlatform.system}.default;
      description = "The installed Verdict package (server and CLI).";
    };

    user = lib.mkOption {
      type = lib.types.str;
      description = "Login user whose session runs the server and owns its state.";
      example = "local-user";
    };

    bind = lib.mkOption {
      type = lib.types.str;
      default = "127.0.0.1";
      description = "Address the server listens on. Use a private interface's IP to keep it off the public network.";
      example = "100.64.0.2";
    };

    port = lib.mkOption {
      type = lib.types.port;
      default = 7071;
      description = "TCP port the server listens on.";
    };

    publicUrl = lib.mkOption {
      type = lib.types.nullOr lib.types.str;
      default = null;
      description = ''
        Origin clients on this machine should send reviews to; exported
        system-wide as VERDICT_URL so `verdict publish` works from any shell.
      '';
      example = "http://host.example.ts.net:7071";
    };

    stateDir = lib.mkOption {
      type = lib.types.str;
      default = "/Users/${cfg.user}/.local/state/verdict";
      defaultText = lib.literalExpression ''"/Users/''${user}/.local/state/verdict"'';
      description = "Reviews and the log (XDG state dir), exported as VERDICT_STATE_DIR.";
    };

    environment = lib.mkOption {
      type = lib.types.attrsOf lib.types.str;
      default = { };
      description = "Extra server settings (VERDICT_* variables, e.g. VERDICT_STALE_DAYS).";
    };

    label = lib.mkOption {
      type = lib.types.str;
      default = "org.example.verdict";
      description = "launchd Label; immutable once shipped.";
    };
  };

  config = lib.mkIf cfg.enable {
    environment.systemPackages = [ cfg.package ];
    environment.variables = lib.optionalAttrs (cfg.publicUrl != null) {
      VERDICT_URL = cfg.publicUrl;
    };

    system.activationScripts.postActivation.text = lib.mkAfter ''
      # launchd opens StandardOutPath before the server runs; create the state
      # dir as the user so no parent (~/.local/state) ends up owned by root.
      /usr/bin/sudo -u ${lib.escapeShellArg cfg.user} /bin/mkdir -p ${lib.escapeShellArg cfg.stateDir}
    '';

    launchd.user.agents.verdict.serviceConfig = {
      Label = cfg.label;
      ProgramArguments = [ exe "serve" ];
      EnvironmentVariables = {
        HOME = "/Users/${cfg.user}";
        VERDICT_BIND = cfg.bind;
        VERDICT_PORT = toString cfg.port;
        VERDICT_STATE_DIR = cfg.stateDir;
      } // cfg.environment;
      RunAtLoad = true;
      KeepAlive = true;
      # A bind that fails (interface not up yet) retries every 30 s.
      ThrottleInterval = 30;
      ProcessType = "Background";
      WorkingDirectory = cfg.stateDir;
      StandardOutPath = "${cfg.stateDir}/verdict.log";
      StandardErrorPath = "${cfg.stateDir}/verdict.log";
    };
  };
}
