const contextmenus = {};

contextmenus.hide_open_menus = function()
{
    while (true)
    {
        const menus = document.getElementsByClassName("open_contextmenu");
        if (!menus.length)
        {
            break;
        }
        menus[0].classList.remove("open_contextmenu");
    }
};

contextmenus.menu_is_open = function()
{
    return document.getElementsByClassName("open_contextmenu").length > 0;
};

contextmenus.show_menu = function(event, menu)
{
    contextmenus.hide_open_menus();
    menu.classList.add("open_contextmenu");
    const html = document.documentElement;
    const over_right = Math.max(0, event.clientX + menu.offsetWidth - html.clientWidth);
    const over_bottom = Math.max(0, event.clientY + menu.offsetHeight - html.clientHeight);
    menu.style.left = (event.clientX - over_right) + "px";
    menu.style.top = (event.pageY - over_bottom) + "px";
};

contextmenus.background_click = function(event)
{
    if (!event.target.closest(".contextmenu"))
    {
        contextmenus.hide_open_menus();
    }
};

document.addEventListener("DOMContentLoaded", function()
{
    document.body.addEventListener("click", contextmenus.background_click);
});
