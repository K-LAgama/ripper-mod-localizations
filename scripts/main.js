import {initConfig} from "./config.js";
import {showWelcome} from "./lib/welcome.js";
import { registerSettings } from "./settings.js";

export const MODULE_ID = "module-id";

export const API = {};

Hooks.on("init", () => {
    registerSettings();
    const module = game.modules.get(MODULE_ID);
    module.API = API;
});

Hooks.on("ready", () => {
    showWelcome();
    initConfig();
});