const common = {};

common.INPUT_TYPES = new Set(["INPUT", "TEXTAREA", "SELECT"]);

common.delete_all_children = function delete_all_children(element)
{
    while (element.firstChild)
    {
        element.removeChild(element.firstChild);
    }
};

common.html_to_element = function html_to_element(html)
{
    const template = document.createElement("template");
    template.innerHTML = html.trim();
    return template.content.firstElementChild;
};

common.bind_box_to_button = function bind_box_to_button(box, button, ctrl_enter)
{
    box.addEventListener("keydown", function(event)
    {
        if (event.key !== "Enter")
        {
            return;
        }
        if (ctrl_enter && !event.ctrlKey)
        {
            return;
        }
        event.preventDefault();
        button.click();
    });
};

common.init_button_with_confirm = function init_button_with_confirm(button)
{
    const action = button.dataset.onclick;
    if (!action)
    {
        return;
    }
    button.removeAttribute("onclick");
    button.removeAttribute("data-onclick");
    button.dataset.confirmArmed = "";

    const originalText = button.innerText;
    const reset = function()
    {
        button.innerText = originalText;
        delete button.dataset.confirmArmed;
    };

    button.onclick = function(event)
    {
        event.stopPropagation();
        if (!button.dataset.confirmArmed)
        {
            button.dataset.confirmArmed = "1";
            button.innerText = (button.dataset.prompt || "Are you sure?") + " (click again)";
            setTimeout(reset, 5000);
            return false;
        }
        reset();
        return Function("event", action)(event);
    };
};

common.init_all_button_with_confirm = function()
{
    for (const button of document.getElementsByClassName("button_with_confirm"))
    {
        common.init_button_with_confirm(button);
    }
};

common.init_all_input_bind_to_button = function()
{
    for (const input of document.querySelectorAll("*[data-bind-enter-to-button]"))
    {
        const button = document.getElementById(input.dataset.bindEnterToButton);
        if (button)
        {
            common.bind_box_to_button(input, button, false);
            delete input.dataset.bindEnterToButton;
        }
    }
    for (const input of document.querySelectorAll("*[data-bind-ctrl-enter-to-button]"))
    {
        const button = document.getElementById(input.dataset.bindCtrlEnterToButton);
        if (button)
        {
            common.bind_box_to_button(input, button, true);
            delete input.dataset.bindCtrlEnterToButton;
        }
    }
};

common.init_all_enable_on_pageload = function()
{
    for (const element of document.getElementsByClassName("enable_on_pageload"))
    {
        element.disabled = false;
        element.classList.remove("enable_on_pageload");
    }
};

common.format_timestamp_local = function format_timestamp_local(timestamp)
{
    if (timestamp === null || timestamp === undefined)
    {
        return "";
    }
    const date = new Date(timestamp * 1000);
    return date.toLocaleString();
};

common.format_size = function format_size(nbytes)
{
    if (nbytes === null || nbytes === undefined)
    {
        return "";
    }
    nbytes = Number(nbytes);
    const units = ["B", "KB", "MB", "GB"];
    let index = 0;
    while (nbytes >= 1024 && index < units.length - 1)
    {
        nbytes /= 1024;
        index += 1;
    }
    if (index === 0)
    {
        return `${nbytes} B`;
    }
    return `${nbytes.toFixed(1)} ${units[index]}`;
};

common.refresh = function refresh()
{
    window.location.reload();
};

common.on_pageload = function()
{
    common.init_all_button_with_confirm();
    common.init_all_input_bind_to_button();
    common.init_all_enable_on_pageload();
};

document.addEventListener("DOMContentLoaded", common.on_pageload);
