import base from "./playwright.config.mjs";

export default { ...base, use: { ...base.use, channel: "chrome" } };
