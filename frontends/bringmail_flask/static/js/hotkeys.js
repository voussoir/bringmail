const hotkeys = {};

hotkeys.HOTKEYS = {};
hotkeys.HELPS = [];

hotkeys.identifier = function(key, ctrlKey, shiftKey, altKey)
{
    return key.toLowerCase() + "." + (ctrlKey & 1) + "." + (shiftKey & 1) + "." + (altKey & 1);
};

hotkeys.human = function(key, ctrlKey, shiftKey, altKey)
{
    let mods = [];
    if (ctrlKey)
    {
        mods.push("CTRL");
    }
    if (shiftKey)
    {
        mods.push("SHIFT");
    }
    if (altKey)
    {
        mods.push("ALT");
    }
    mods = mods.join("+");
    if (mods)
    {
        mods += "+";
    }
    return mods + key.toUpperCase();
};

hotkeys.register_hotkey = function(hotkey, action, description)
{
    if (!Array.isArray(hotkey))
    {
        hotkey = hotkey.split(/\s+/g);
    }
    const key = hotkey.pop();
    const modifiers = hotkey.map(word => word.toLowerCase());
    const ctrlKey = modifiers.includes("ctrl") || modifiers.includes("control");
    const shiftKey = modifiers.includes("shift");
    const altKey = modifiers.includes("alt");
    const identifier = hotkeys.identifier(key, ctrlKey, shiftKey, altKey);
    hotkeys.HOTKEYS[identifier] = {
        action: action,
        human: hotkeys.human(key, ctrlKey, shiftKey, altKey),
        description: description,
    };
};

hotkeys.register_help = function(help)
{
    hotkeys.HELPS.push(help);
};

hotkeys.should_ignore = function(event)
{
    if (event.target.tagName === "INPUT" && event.target.type === "checkbox")
    {
        return false;
    }
    return common.INPUT_TYPES.has(event.target.tagName);
};

hotkeys.show_all = function()
{
    const lines = [];
    for (const identifier in hotkeys.HOTKEYS)
    {
        const entry = hotkeys.HOTKEYS[identifier];
        lines.push(`${entry.human}: ${entry.description}`);
    }
    if (hotkeys.HELPS.length)
    {
        lines.push("");
        lines.push(...hotkeys.HELPS);
    }
    alert(lines.join("\n"));
};

hotkeys.listener = function(event)
{
    if (hotkeys.should_ignore(event))
    {
        return;
    }
    const identifier = hotkeys.identifier(event.key, event.ctrlKey, event.shiftKey, event.altKey);
    const entry = hotkeys.HOTKEYS[identifier];
    if (!entry)
    {
        return;
    }
    entry.action(event);
    event.preventDefault();
};

window.addEventListener("keydown", hotkeys.listener);
hotkeys.register_hotkey("/", hotkeys.show_all, "Show hotkeys.");
