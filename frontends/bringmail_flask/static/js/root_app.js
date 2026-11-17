"use strict";

const BLOCKED_IMAGE = "data:image/svg+xml;charset=utf-8," + encodeURIComponent(
    '<svg xmlns="http://www.w3.org/2000/svg" width="240" height="32">' +
    '<rect width="100%" height="100%" fill="#eeeeee"/>' +
    '<text x="8" y="21" font-family="sans-serif" font-size="12" fill="#666666">Remote image blocked</text>' +
    '</svg>'
);

const MESSAGE_PAGE_SIZE = 500;

let accounts_element = null;
let messages_element = null;
let filter_input = null;
let active_title_element = null;
let accounts_spinner = null;
let news_spinner = null;
let reader_spinner = null;
let messages_loading = false;
let messages_request_serial = 0;
let refresh_reload_pending = false;

let reader_subject = null;
let reader_meta = null;
let reader_attachments = null;
let reader_iframe = null;
let reader_plain = null;
let reader_headers = null;
let reader_body = null;
let reader_html_button = null;
let reader_text_button = null;
let reader_images_button = null;
let reader_headers_button = null;

let context_menu = null;
let context_title = null;
let context_refresh_button = null;
let context_settings_link = null;
let context_target = null;

let accounts_data = [];
let active_kind = null;
let active_id = null;
let active_account_id = null;
let active_folder_id = null;

let messages_data = [];
let messages_rendered = 0;
let messages_has_more = false;
let selected_ids = new Set();
let selection_anchor_id = null;
// Message ids that were trashed from this view and are still shown in place
// with the strikethrough class until the view is reloaded.
let trashed_ids = new Set();
let messages_request = null;

let current_message_id = null;
let current_message_body = null;
let reader_request = null;
let reader_resize_observer = null;
let reader_mode = "text";
let reader_show_headers = false;
let reader_headers_text = null;
let allow_images = false;

let sse = null;
let sse_watchdog_timeout = null;
let dynamic_filter_timeout = null;
let accounts_reload_timeout = null;
let status_text_element = null;
let status_detail_element = null;
let status_timestamp_element = null;
let status_spinner = null;
let refresh_status = null;


////////////////////////////////////////////////////////////////////////////////////////////////////
// SMALL HELPERS

function api_was_ok(response) {
    return (
        response.meta.completed &&
        response.meta.json_ok &&
        response.meta.status === 200
    );
}

function show_api_error(response) {
    if (response && response.data && response.data.error_message) {
        alert(`${response.data.error_type || "Error"}: ${response.data.error_message}`);
    } else {
        alert("The request failed. See the console for details.");
        console.error(response);
    }
}

function find_account(account_id) {
    return accounts_data.find(account => String(account.id) === String(account_id)) || null;
}

function find_folder(folder_id) {
    for (const account of accounts_data) {
        for (const folder of account.folders) {
            if (String(folder.id) === String(folder_id)) {
                return folder;
            }
        }
    }
    return null;
}

function find_message(message_id) {
    return messages_data.find(message => String(message.id) === String(message_id)) || null;
}

function message_row(message_id) {
    return messages_element.querySelector(`.message[data-id="${message_id}"]`);
}

function format_addresses(addresses) {
    if (!addresses || addresses.length === 0) {
        return "";
    }
    return addresses.map(address => {
        if (address.name && address.addr) {
            return `${address.name} <${address.addr}>`;
        }
        return address.name || address.addr || "";
    }).filter(value => value).join(", ");
}

function set_unread_span(element, count) {
    if (!element) {
        return;
    }
    count = Math.max(0, count || 0);
    element.textContent = count;
    if (count === 0) {
        element.classList.add("hidden");
    } else {
        element.classList.remove("hidden");
    }
}

function account_icon() {
    return "📮";
}

function folder_icon(folder) {
    const icons = {
        "\\Inbox": "📥",
        "\\Sent": "📤",
        "\\Drafts": "📝",
        "\\Trash": "🗑",
        "\\Junk": "⚠",
        "\\Archive": "🗄",
    };
    return icons[folder.special_use] || "📁";
}

function set_active_title(text) {
    active_title_element.textContent = text || "";
}


////////////////////////////////////////////////////////////////////////////////////////////////////
// ACCOUNTS TREE

function load_accounts(callback) {
    accounts_spinner.show(100);
    api.accounts.get_accounts(function(response) {
        accounts_spinner.hide();
        if (!api_was_ok(response)) {
            show_api_error(response);
            return;
        }
        accounts_data = response.data;
        render_accounts();
        if (callback) {
            callback();
        }
    });
}

function render_accounts() {
    common.delete_all_children(accounts_element);

    const all_div = document.createElement("div");
    all_div.classList.add("account", "all_accounts");
    const all_header = document.createElement("div");
    all_header.classList.add("account_header");
    all_header.dataset.allAccounts = "1";

    const all_icon = document.createElement("span");
    all_icon.classList.add("icon");
    all_icon.textContent = "📬";
    all_header.appendChild(all_icon);

    const all_title = document.createElement("span");
    all_title.classList.add("title");
    all_title.textContent = "All accounts";
    all_header.appendChild(all_title);

    const all_unread = document.createElement("span");
    all_unread.classList.add("unread_count");
    all_unread.dataset.allAccountsUnread = "1";
    all_header.appendChild(all_unread);

    all_header.addEventListener("click", function() {
        select_all_accounts();
    });
    all_div.appendChild(all_header);
    accounts_element.appendChild(all_div);

    for (const account of accounts_data) {
        const account_div = document.createElement("div");
        account_div.classList.add("account");
        account_div.dataset.accountId = account.id;
        if (localStorage.getItem(`bringmail_collapse_${account.id}`)) {
            account_div.classList.add("collapsed");
        }

        const header = document.createElement("div");
        header.classList.add("account_header");
        header.dataset.accountId = account.id;

        const icon = document.createElement("span");
        icon.classList.add("icon");
        icon.textContent = account_icon();
        header.appendChild(icon);

        const title = document.createElement("span");
        title.classList.add("title");
        title.textContent = account.display_name;
        if (account.last_refresh_error) {
            title.classList.add("refresh_error");
            title.title = account.last_refresh_error;
        }
        header.appendChild(title);

        const unread = document.createElement("span");
        unread.classList.add("unread_count");
        unread.dataset.accountUnread = account.id;
        set_unread_span(unread, account.unread_count);
        header.appendChild(unread);

        header.addEventListener("click", function() {
            select_account(account.id);
        });
        header.addEventListener("dblclick", function(event) {
            event.stopPropagation();
            toggle_account_collapsed(account_div, account.id);
        });
        header.addEventListener("contextmenu", function(event) {
            show_account_contextmenu(event, "account", account.id, account.id, account.display_name);
        });

        account_div.appendChild(header);

        const folders_div = document.createElement("div");
        folders_div.classList.add("folders");
        for (const folder of account.folders) {
            const folder_div = document.createElement("div");
            folder_div.classList.add("folder");
            folder_div.dataset.folderId = folder.id;
            folder_div.dataset.accountId = account.id;
            folder_div.style.paddingLeft = `${4 + (folder.depth || 0) * 14}px`;

            const folder_icon_element = document.createElement("span");
            folder_icon_element.classList.add("icon");
            folder_icon_element.textContent = folder_icon(folder);
            folder_div.appendChild(folder_icon_element);

            const folder_title = document.createElement("span");
            folder_title.classList.add("title");
            folder_title.textContent = folder.display_name;
            if (folder.last_sync_error) {
                folder_title.classList.add("refresh_error");
                folder_title.title = folder.last_sync_error;
            }
            folder_div.appendChild(folder_title);

            const folder_unread = document.createElement("span");
            folder_unread.classList.add("unread_count");
            folder_unread.dataset.folderUnread = folder.id;
            set_unread_span(folder_unread, folder.unread_count);
            folder_div.appendChild(folder_unread);

            folder_div.addEventListener("click", function(event) {
                event.stopPropagation();
                select_folder(folder.id);
            });
            folder_div.addEventListener("contextmenu", function(event) {
                show_account_contextmenu(event, "folder", folder.id, account.id, folder.display_name);
            });

            folders_div.appendChild(folder_div);
        }
        account_div.appendChild(folders_div);
        accounts_element.appendChild(account_div);
    }

    update_active_classes();
    update_unread_displays();
    build_move_options();
}

function toggle_account_collapsed(account_div, account_id) {
    if (account_div.classList.contains("collapsed")) {
        account_div.classList.remove("collapsed");
        localStorage.removeItem(`bringmail_collapse_${account_id}`);
    } else {
        account_div.classList.add("collapsed");
        localStorage.setItem(`bringmail_collapse_${account_id}`, "1");
    }
}

function update_unread_displays() {
    let total_unread = 0;
    for (const account of accounts_data) {
        total_unread += account.unread_count || 0;
        const account_span = accounts_element.querySelector(`[data-account-unread="${account.id}"]`);
        set_unread_span(account_span, account.unread_count);
        for (const folder of account.folders) {
            const folder_span = accounts_element.querySelector(`[data-folder-unread="${folder.id}"]`);
            set_unread_span(folder_span, folder.unread_count);
        }
    }
    const all_span = accounts_element.querySelector("[data-all-accounts-unread]");
    set_unread_span(all_span, total_unread);
}

function folder_in_account_view(folder, account) {
    const root_names = new Set([
        "INBOX", "SENT", "SENT ITEMS", "SENT MAIL", "SENT MESSAGES",
    ]);
    const roots = account.folders.filter(item => {
        if (item.special_use === "\\Inbox" || item.special_use === "\\Sent") {
            return true;
        }
        const name = (item.name || "").split(/[./]/).pop().toUpperCase();
        return root_names.has(name);
    });
    for (const root of roots) {
        if (String(root.id) === String(folder.id)) {
            return true;
        }
        const delimiter = root.delimiter || folder.delimiter;
        if (delimiter && String(folder.name).startsWith(root.name + delimiter)) {
            return true;
        }
    }
    return false;
}

function folder_in_active_view(folder_id) {
    const folder = find_folder(folder_id);
    if (!folder) {
        return false;
    }
    if (active_kind === "folder") {
        return String(folder.id) === String(active_folder_id);
    }
    if (active_kind === "account") {
        const account = find_account(active_account_id);
        return Boolean(account && folder_in_account_view(folder, account));
    }
    if (active_kind === "all") {
        for (const account of accounts_data) {
            if (folder_in_account_view(folder, account)) {
                return true;
            }
        }
    }
    return false;
}

function adjust_unread(folder_id, delta) {
    for (const account of accounts_data) {
        const folder = account.folders.find(item => String(item.id) === String(folder_id));
        if (!folder) {
            continue;
        }
        folder.unread_count = Math.max(0, (folder.unread_count || 0) + delta);
        if (folder_in_account_view(folder, account)) {
            account.unread_count = Math.max(0, (account.unread_count || 0) + delta);
        }
        update_unread_displays();
        return;
    }
}

function update_active_classes() {
    for (const account_div of accounts_element.querySelectorAll(".account")) {
        account_div.classList.toggle(
            "active",
            String(account_div.dataset.accountId) === String(active_account_id)
        );
    }
    const all_div = accounts_element.querySelector(".all_accounts");
    if (all_div) {
        all_div.classList.toggle("active", active_kind === "all");
    }
    for (const folder_div of accounts_element.querySelectorAll(".folder")) {
        folder_div.classList.toggle(
            "active",
            active_kind === "folder" && String(folder_div.dataset.folderId) === String(active_id)
        );
    }
}

function select_all_accounts() {
    refresh_reload_pending = false;
    active_kind = "all";
    active_id = "all";
    active_account_id = null;
    active_folder_id = null;
    history.replaceState(null, "", "/all");

    update_active_classes();
    set_active_title("All accounts / Inbox + Sent");
    load_messages(true);
    build_move_options();
}

function select_account(account_id) {
    const account = find_account(account_id);
    if (!account) {
        return;
    }
    refresh_reload_pending = false;
    active_kind = "account";
    active_id = account.id;
    active_account_id = account.id;
    active_folder_id = null;
    history.replaceState(null, "", `/account/${account.id}`);

    update_active_classes();
    set_active_title(`${account.display_name} / Inbox + Sent`);
    load_messages(true);
    build_move_options();
}

function select_folder(folder_id) {
    const folder = find_folder(folder_id);
    if (!folder) {
        return;
    }
    if (!folder.selectable) {
        return;
    }
    refresh_reload_pending = false;
    active_kind = "folder";
    active_id = folder.id;
    active_account_id = folder.account_id;
    active_folder_id = folder.id;
    history.replaceState(null, "", `/folder/${folder.id}`);

    const account = find_account(folder.account_id);
    const account_div = accounts_element.querySelector(`.account[data-account-id="${folder.account_id}"]`);
    if (account_div && account_div.classList.contains("collapsed")) {
        toggle_account_collapsed(account_div, folder.account_id);
    }

    update_active_classes();
    set_active_title(account ? `${account.display_name} / ${folder.display_name}` : folder.display_name);
    load_messages(true);
    build_move_options();
}

function build_move_options() {
    const select = document.getElementById("move_select");
    common.delete_all_children(select);
    const first = document.createElement("option");
    first.value = "";
    first.textContent = "Move to...";
    select.appendChild(first);

    const account = find_account(active_account_id);
    if (!account) {
        select.disabled = true;
        return;
    }
    select.disabled = false;
    // A trashed message is no longer in the active folder, so that folder has
    // to remain a valid destination while trashed rows are still on screen.
    const include_active_folder = active_kind === "folder" && trashed_ids.size > 0;
    for (const folder of account.folders) {
        if (!folder.selectable) {
            continue;
        }
        if (
            active_kind === "folder" &&
            String(folder.id) === String(active_id) &&
            !include_active_folder
        ) {
            continue;
        }
        const option = document.createElement("option");
        option.value = folder.id;
        option.textContent = folder.name;
        select.appendChild(option);
    }
}


////////////////////////////////////////////////////////////////////////////////////////////////////
// MESSAGE LIST

function load_messages(reset) {
    if (active_kind === null || active_id === null) {
        return;
    }
    if (!reset && messages_loading) {
        return;
    }
    if (reset) {
        messages_data = [];
        messages_rendered = 0;
        messages_has_more = false;
        selected_ids = new Set();
        selection_anchor_id = null;
        trashed_ids = new Set();
        common.delete_all_children(messages_element);
        messages_element.scrollTop = 0;
        clear_reader();
    }

    const parameters = {limit: MESSAGE_PAGE_SIZE};
    if (!reset && messages_data.length) {
        const last = messages_data[messages_data.length - 1];
        parameters.before_date = last.date_unix;
        parameters.before_id = last.id;
    }

    const serial = ++messages_request_serial;
    messages_loading = true;
    if (messages_request) {
        messages_request.abort();
    }
    news_spinner.show(100);

    function callback(response) {
        if (serial !== messages_request_serial) {
            return;
        }
        messages_loading = false;
        news_spinner.hide();
        if (!response.meta.completed) {
            return;
        }
        if (!api_was_ok(response)) {
            show_api_error(response);
            return;
        }
        const data = response.data;
        if (reset) {
            messages_data = [];
            messages_rendered = 0;
        }
        messages_data.push(...data.messages);
        messages_has_more = data.has_more;
        render_messages(reset);
        setTimeout(maybe_load_more_messages, 0);
    }

    if (active_kind === "folder") {
        messages_request = api.folders.get_messages(active_id, parameters, callback);
    } else if (active_kind === "account") {
        messages_request = api.accounts.get_messages(active_id, parameters, callback);
    } else if (active_kind === "all") {
        messages_request = api.accounts.get_all_messages(parameters, callback);
    }
}

function maybe_load_more_messages() {
    if (
        active_kind === null ||
        !messages_has_more ||
        messages_loading
    ) {
        return;
    }
    const remaining = (
        messages_element.scrollHeight -
        messages_element.scrollTop -
        messages_element.clientHeight
    );
    if (remaining <= 300) {
        load_messages(false);
    }
}

function create_message_row(message) {
    const row = document.createElement("div");
    row.classList.add("message");
    row.dataset.id = message.id;
    row.dataset.folderId = message.folder_id;
    row.dataset.date = message.date_unix;
    if (!message.seen) {
        row.classList.add("unread");
    }
    if (trashed_ids.has(String(message.id))) {
        row.classList.add("trashed");
    }
    if (selected_ids.has(String(message.id))) {
        row.classList.add("message_selected");
    }

    const read_indicator = document.createElement("span");
    read_indicator.classList.add("read_indicator");
    read_indicator.textContent = "●";
    read_indicator.title = message.seen ? "Mark unread" : "Mark read";
    read_indicator.setAttribute("role", "button");
    row.appendChild(read_indicator);

    const from = document.createElement("span");
    from.classList.add("from");
    from.textContent = message.from_display || "";
    from.title = format_addresses(message.from);
    row.appendChild(from);

    const subject = document.createElement("span");
    subject.classList.add("subject");
    subject.textContent = message.subject || "(no subject)";
    subject.title = message.snippet || "";
    row.appendChild(subject);

    const attachment = document.createElement("span");
    attachment.classList.add("attachment");
    attachment.textContent = message.has_attachments ? "📎" : "";
    row.appendChild(attachment);

    const date = document.createElement("span");
    date.classList.add("date");
    date.textContent = common.format_timestamp_local(message.date_unix);
    row.appendChild(date);

    return row;
}

function render_messages(reset) {
    if (reset) {
        common.delete_all_children(messages_element);
        messages_rendered = 0;
    }
    const fragment = document.createDocumentFragment();
    for (let index = messages_rendered; index < messages_data.length; index += 1) {
        fragment.appendChild(create_message_row(messages_data[index]));
    }
    messages_element.appendChild(fragment);
    messages_rendered = messages_data.length;

    if (messages_data.length === 0) {
        const empty = document.createElement("div");
        empty.classList.add("empty_message");
        empty.textContent = "No messages in this view.";
        messages_element.appendChild(empty);
    }

    apply_filter();
}

function get_visible_rows() {
    return Array.from(messages_element.querySelectorAll(".message:not(.hidden)"));
}

function get_selected_ids() {
    return Array.from(messages_element.querySelectorAll(".message_selected:not(.hidden)"))
        .map(row => row.dataset.id);
}

function update_selection_classes() {
    for (const row of messages_element.querySelectorAll(".message")) {
        row.classList.toggle("message_selected", selected_ids.has(String(row.dataset.id)));
    }
}

function select_only(message_id) {
    selected_ids = new Set([String(message_id)]);
    selection_anchor_id = String(message_id);
    update_selection_classes();
}

function toggle_ctrl_selection(message_id) {
    message_id = String(message_id);
    if (selected_ids.has(message_id)) {
        selected_ids.delete(message_id);
    } else {
        selected_ids.add(message_id);
    }
    selection_anchor_id = message_id;
    update_selection_classes();
}

function select_range(message_id) {
    const rows = get_visible_rows();
    const target_index = rows.findIndex(row => String(row.dataset.id) === String(message_id));
    const anchor_index = rows.findIndex(row => String(row.dataset.id) === String(selection_anchor_id));
    if (target_index === -1 || anchor_index === -1) {
        select_only(message_id);
        return;
    }
    const start = Math.min(target_index, anchor_index);
    const end = Math.max(target_index, anchor_index);
    selected_ids = new Set();
    for (let index = start; index <= end; index += 1) {
        selected_ids.add(String(rows[index].dataset.id));
    }
    update_selection_classes();
}

function on_messages_click(event) {
    if (event.target.classList.contains("read_indicator")) {
        const dot_row = event.target.closest(".message");
        if (!dot_row) {
            return;
        }
        event.stopPropagation();
        toggle_message_seen(dot_row.dataset.id);
        return;
    }

    const row = event.target.closest(".message");
    if (!row) {
        return;
    }
    const message_id = row.dataset.id;

    if (event.ctrlKey) {
        toggle_ctrl_selection(message_id);
    } else if (event.shiftKey) {
        select_range(message_id);
    } else {
        select_only(message_id);
    }

    if (get_selected_ids().length === 1 && selected_ids.has(String(message_id))) {
        show_reader(message_id);
    } else if (get_selected_ids().length === 0) {
        clear_reader();
    }
}

function toggle_message_seen(message_id) {
    const message = find_message(message_id);
    if (!message) {
        return;
    }
    const seen = !message.seen;
    api.messages.set_seen(message.id, seen, function(response) {
        if (!api_was_ok(response)) {
            show_api_error(response);
            return;
        }
        set_seen_local(message.id, seen);
    });
}

function set_seen_local(message_id, seen) {
    const message = find_message(message_id);
    const row = message_row(message_id);
    if (!message) {
        return;
    }
    const was_unread = !message.seen;
    message.seen = seen;
    if (row) {
        row.classList.toggle("unread", !seen);
        const indicator = row.querySelector(".read_indicator");
        if (indicator) {
            indicator.title = seen ? "Mark unread" : "Mark read";
        }
    }
    if (was_unread && seen) {
        adjust_unread(message.folder_id, -1);
    } else if (!was_unread && !seen) {
        adjust_unread(message.folder_id, +1);
    }
}

function trash_folder_for_account(account_id) {
    const account = find_account(account_id);
    if (!account) {
        return null;
    }
    const folder = account.folders.find(item => item.special_use === "\\Trash");
    return folder ? folder.id : null;
}

function mark_trashed_local(message_id) {
    message_id = String(message_id);
    trashed_ids.add(message_id);
    const message = find_message(message_id);
    if (message) {
        const trash_folder_id = trash_folder_for_account(message.account_id);
        if (trash_folder_id !== null) {
            message.folder_id = trash_folder_id;
        }
    }
    const row = message_row(message_id);
    if (row) {
        row.classList.add("trashed");
    }
}

function untrash_local(message_id, folder_id) {
    message_id = String(message_id);
    trashed_ids.delete(message_id);
    const message = find_message(message_id);
    if (message && folder_id !== undefined && folder_id !== null) {
        message.folder_id = folder_id;
    }
    const row = message_row(message_id);
    if (row) {
        row.classList.remove("trashed");
    }
}

function apply_filter() {
    const searchterm = (filter_input.value || "").trim().toLocaleLowerCase();
    for (const row of messages_element.querySelectorAll(".message")) {
        const message = find_message(row.dataset.id);
        if (!message) {
            continue;
        }
        const text = [
            message.from_display,
            message.subject,
            message.snippet,
            format_addresses(message.from),
        ].join(" ").toLocaleLowerCase();
        row.classList.toggle("hidden", searchterm !== "" && text.indexOf(searchterm) === -1);
    }
}

function select_all_visible() {
    for (const row of get_visible_rows()) {
        selected_ids.add(String(row.dataset.id));
    }
    update_selection_classes();
}

function navigate(direction) {
    const rows = get_visible_rows();
    if (rows.length === 0) {
        return;
    }
    let index = rows.findIndex(row => selected_ids.has(String(row.dataset.id)));
    if (index === -1) {
        index = 0;
    } else {
        index += direction;
    }
    index = Math.max(0, Math.min(index, rows.length - 1));
    const row = rows[index];
    select_only(row.dataset.id);
    row.scrollIntoView({block: "nearest"});
    show_reader(row.dataset.id);
}


////////////////////////////////////////////////////////////////////////////////////////////////////
// READER

function clear_reader() {
    current_message_id = null;
    current_message_body = null;
    reader_mode = "text";
    reader_show_headers = false;
    reader_headers_text = null;
    allow_images = false;

    if (reader_request) {
        reader_request.abort();
        reader_request = null;
    }
    reader_spinner.hide();
    reader_subject.textContent = "";
    reader_meta.textContent = "";
    common.delete_all_children(reader_attachments);
    reader_iframe.srcdoc = "";
    reader_iframe.style.height = "";
    reader_iframe.classList.add("hidden");
    reader_plain.textContent = "";
    reader_headers.textContent = "";
    reader_plain.classList.add("hidden");
    reader_headers.classList.add("hidden");
    reader_html_button.classList.add("hidden");
    reader_text_button.classList.add("hidden");
    reader_images_button.classList.add("hidden");
    reader_headers_button.classList.add("hidden");
}

function show_reader(message_id) {
    const message = find_message(message_id);
    if (!message) {
        return;
    }
    if (current_message_id === String(message_id)) {
        return;
    }
    current_message_id = String(message_id);
    current_message_body = null;
    allow_images = false;
    reader_show_headers = false;
    reader_headers_text = null;

    reader_subject.textContent = message.subject || "(no subject)";
    reader_meta.textContent = "";
    common.delete_all_children(reader_attachments);
    reader_iframe.srcdoc = "";
    reader_iframe.style.height = "";
    reader_iframe.classList.add("hidden");
    reader_plain.textContent = "";
    reader_headers.textContent = "";
    reader_plain.classList.add("hidden");
    reader_headers.classList.add("hidden");
    reader_html_button.classList.add("hidden");
    reader_text_button.classList.add("hidden");
    reader_images_button.classList.add("hidden");
    reader_headers_button.classList.add("hidden");
    reader_spinner.show(150);

    if (reader_request) {
        reader_request.abort();
    }

    function callback(response) {
        if (!response.meta.completed) {
            return;
        }
        if (String(message_id) !== current_message_id) {
            return;
        }
        reader_spinner.hide();
        if (!api_was_ok(response)) {
            show_api_error(response);
            return;
        }
        current_message_body = response.data;
        render_reader(response.data);
        mark_message_read(message_id);
    }

    reader_request = api.messages.get(message_id, callback);
}

function mark_message_read(message_id) {
    const message = find_message(message_id);
    if (!message || message.seen) {
        return;
    }
    set_seen_local(message_id, true);
    api.messages.set_seen(message_id, true, function(response) {
        if (!api_was_ok(response)) {
            set_seen_local(message_id, false);
            console.warn("Could not mark message as read on the server.");
        }
    });
}

function render_reader(body) {
    reader_subject.textContent = body.subject || "(no subject)";

    const from = format_addresses(body.from);
    const to = format_addresses(body.to);
    const cc = format_addresses(body.cc);
    const date = common.format_timestamp_local(body.date_unix);
    let meta = "";
    if (from) { meta += `From: ${from}\n`; }
    if (to) { meta += `To: ${to}\n`; }
    if (cc) { meta += `Cc: ${cc}\n`; }
    if (date) { meta += `Date: ${date}`; }
    reader_meta.textContent = meta.trim();
    reader_meta.style.whiteSpace = "pre-wrap";

    common.delete_all_children(reader_attachments);
    for (const attachment of body.attachments || []) {
        const link = document.createElement("a");
        link.href = `/message/${body.id}/part/${attachment.index}`;
        link.download = attachment.filename || "";
        const label = attachment.filename || `part ${attachment.index + 1}`;
        link.textContent = `${label} (${common.format_size(attachment.size)})`;
        link.title = attachment.content_type || "";
        reader_attachments.appendChild(link);
    }

    reader_html_button.classList.toggle("hidden", !body.html);
    reader_text_button.classList.toggle("hidden", !body.text);
    reader_images_button.classList.toggle("hidden", !body.remote_content);
    reader_headers_button.classList.remove("hidden");

    reader_mode = body.text ? "text" : "html";
    reader_show_headers = false;
    allow_images = false;

    render_reader_body();
}

function render_reader_body() {
    if (!current_message_body) {
        return;
    }
    const body = current_message_body;

    reader_html_button.classList.toggle("bold", reader_mode === "html" && !reader_show_headers);
    reader_text_button.classList.toggle("bold", reader_mode === "text" && !reader_show_headers);
    if (allow_images || !body.remote_content || reader_mode !== "html") {
        reader_images_button.classList.add("hidden");
    } else {
        reader_images_button.classList.remove("hidden");
    }
    reader_headers_button.textContent = reader_show_headers ? "Message" : "Headers";

    if (reader_show_headers) {
        reader_plain.classList.add("hidden");
        reader_iframe.classList.add("hidden");
        reader_headers.classList.remove("hidden");
        reader_headers.textContent = reader_headers_text || "Loading headers...";
        return;
    }

    reader_headers.classList.add("hidden");

    if (reader_mode === "text" || !body.html) {
        reader_iframe.classList.add("hidden");
        reader_plain.classList.remove("hidden");
        reader_plain.textContent = body.text || "(no text body)";
        return;
    }

    reader_plain.classList.add("hidden");
    reader_iframe.classList.remove("hidden");
    render_html_iframe(body);
}

function render_html_iframe(body) {
    const doc = new DOMParser().parseFromString(body.html || "", "text/html");
    const inline_images = body.inline_images || {};

    for (const image of doc.querySelectorAll("img")) {
        let src = image.getAttribute("src");
        if (src && src.toLowerCase().startsWith("cid:")) {
            let cid = src.slice(4).replace(/^<|>$/g, "");
            let part_index = inline_images[cid];
            if (part_index === undefined) {
                try {
                    part_index = inline_images[decodeURIComponent(cid)];
                } catch (exc) {
                    part_index = undefined;
                }
            }
            if (part_index !== undefined) {
                image.setAttribute("src", `/message/${body.id}/part/${part_index}`);
                src = image.getAttribute("src");
            }
        }
    }

    if (!allow_images) {
        for (const element of doc.querySelectorAll("img, source, video, audio")) {
            const src = element.getAttribute("src");
            if (src && /^(https?:)?\/\//i.test(src)) {
                if (element.tagName === "IMG") {
                    element.setAttribute("data-blocked-src", src);
                    element.setAttribute("src", BLOCKED_IMAGE);
                } else {
                    element.removeAttribute("src");
                }
            }
            element.removeAttribute("srcset");
            element.removeAttribute("poster");
        }
        for (const element of doc.querySelectorAll("[style]")) {
            const style = element.getAttribute("style") || "";
            if (/url\(\s*['"]?(https?:)?\/\//i.test(style)) {
                element.removeAttribute("style");
            }
        }
    }

    const img_sources = allow_images ? "data: 'self' https: http:" : "data: 'self'";
    const media_sources = allow_images ? "data: 'self' https: http:" : "'none'";
    const csp = [
        "default-src 'none'",
        "img-src " + img_sources,
        "style-src 'unsafe-inline'",
        "font-src 'none'",
        "media-src " + media_sources,
        "object-src 'none'",
        "base-uri 'none'",
    ].join("; ") + ";";

    const inner = (doc.head ? doc.head.innerHTML : "") + (doc.body ? doc.body.innerHTML : "");
    const html = `<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<meta http-equiv="Content-Security-Policy" content="${csp}">
<style>
html, body { margin: 0; padding: 0; background: #fff; color: #111; }
body { font-family: sans-serif; font-size: 10pt; overflow-wrap: break-word; padding: 8px; }
img { max-width: 100%; height: auto; }
table { max-width: 100%; }
a { color: #0044cc; }
</style>
</head>
<body>${inner}</body>
</html>`;
    reader_iframe.srcdoc = html;
}

function resize_reader_iframe() {
    const doc = reader_iframe.contentDocument;
    if (!doc || !doc.body) {
        return;
    }
    const height = Math.max(200, Math.min(doc.body.scrollHeight + 24, 4000));
    reader_iframe.style.height = `${height}px`;

    if (reader_resize_observer) {
        reader_resize_observer.disconnect();
        reader_resize_observer = null;
    }
    if (window.ResizeObserver) {
        reader_resize_observer = new ResizeObserver(function() {
            const height = Math.max(200, Math.min(doc.body.scrollHeight + 24, 4000));
            reader_iframe.style.height = `${height}px`;
        });
        reader_resize_observer.observe(doc.body);
    }
}

function toggle_headers() {
    if (reader_show_headers) {
        reader_show_headers = false;
        render_reader_body();
        return;
    }
    if (reader_headers_text !== null) {
        reader_show_headers = true;
        render_reader_body();
        return;
    }
    const message_id = current_message_id;
    api.messages.get_headers(message_id, function(response) {
        if (current_message_id !== message_id) {
            return;
        }
        if (!response.meta.completed || response.meta.status !== 200) {
            show_api_error(response);
            return;
        }
        reader_headers_text = response.text || "";
        reader_show_headers = true;
        render_reader_body();
    });
}


////////////////////////////////////////////////////////////////////////////////////////////////////
// TOOLBAR ACTIONS

function selected_or_current_ids() {
    const ids = get_selected_ids();
    if (ids.length > 0) {
        return ids;
    }
    if (current_message_id !== null) {
        return [current_message_id];
    }
    return [];
}

function batch_result_handler(response, action) {
    if (!api_was_ok(response)) {
        show_api_error(response);
        return;
    }
    action(response.data);
}

function mark_selected(seen) {
    const ids = selected_or_current_ids();
    if (ids.length === 0) {
        return;
    }
    api.messages.batch_set_seen(ids, seen, function(response) {
        batch_result_handler(response, function(returned_ids) {
            for (const message_id of returned_ids) {
                set_seen_local(message_id, seen);
            }
        });
    });
}

function remove_messages_locally(message_ids) {
    const id_set = new Set((message_ids || []).map(String));
    if (id_set.size === 0) {
        return;
    }
    messages_data = messages_data.filter(message => !id_set.has(String(message.id)));
    messages_rendered = messages_data.length;
    for (const row of messages_element.querySelectorAll(".message")) {
        if (id_set.has(String(row.dataset.id))) {
            row.remove();
        }
    }
    for (const message_id of id_set) {
        selected_ids.delete(message_id);
        trashed_ids.delete(message_id);
    }
    selection_anchor_id = null;
    if (messages_data.length === 0) {
        render_messages(true);
    } else {
        apply_filter();
    }
}

function move_selected() {
    const select = document.getElementById("move_select");
    const folder_id = select.value;
    if (!folder_id) {
        return;
    }
    const ids = selected_or_current_ids();
    if (ids.length === 0) {
        select.value = "";
        return;
    }

    const previously_trashed = new Set(
        ids.map(String).filter(message_id => trashed_ids.has(String(message_id)))
    );
    const target_folder = find_folder(folder_id);
    const target_is_trash = Boolean(
        target_folder && target_folder.special_use === "\\Trash"
    );
    const target_is_active = (
        active_kind === "folder" &&
        String(folder_id) === String(active_folder_id)
    );

    api.messages.batch_move(ids, folder_id, function(response) {
        select.value = "";
        batch_result_handler(response, function(returned_ids) {
            const to_remove = [];
            for (const message_id of returned_ids) {
                if (previously_trashed.has(String(message_id))) {
                    if (target_is_trash) {
                        // Moving to trash again is a no-op; keep the row trashed.
                        continue;
                    }
                    untrash_local(message_id, folder_id);
                    if (!folder_in_active_view(folder_id)) {
                        to_remove.push(message_id);
                    }
                } else if (!target_is_active) {
                    to_remove.push(message_id);
                }
            }
            if (to_remove.length) {
                clear_reader();
                remove_messages_locally(to_remove);
            }
            build_move_options();
            load_accounts();
        });
    });
}

function delete_selected() {
    const ids = selected_or_current_ids();
    if (ids.length === 0) {
        return;
    }
    // Show the messages as trashed immediately and assume the server request
    // will succeed. The rows stay in place with a strikethrough instead of
    // being removed; the next reload of this view drops them because they now
    // belong to the trash folder.
    const previous_state = new Map();
    for (const message_id of ids) {
        const state = {
            trashed: trashed_ids.has(String(message_id)),
            folder_id: null,
        };
        const message = find_message(message_id);
        if (message) {
            state.folder_id = message.folder_id;
        }
        previous_state.set(String(message_id), state);
        mark_trashed_local(message_id);
    }
    build_move_options();

    api.messages.batch_delete(ids, function(response) {
        if (!api_was_ok(response)) {
            // Roll the optimistic strikethrough back for the rows that were
            // not already trashed before this request.
            for (const [message_id, state] of previous_state) {
                if (state.trashed) {
                    continue;
                }
                untrash_local(message_id, state.folder_id);
            }
            build_move_options();
            show_api_error(response);
            return;
        }
        load_accounts();
    });
}

function refresh_account(account_id) {
    api.accounts.refresh(account_id, function(response) {
        if (!api_was_ok(response)) {
            show_api_error(response);
        }
    });
}

function refresh_active() {
    if (active_kind === "all") {
        refresh_reload_pending = true;
        refresh_all();
        return;
    }
    if (active_account_id !== null) {
        refresh_reload_pending = true;
        refresh_account(active_account_id);
    }
}

function refresh_all() {
    api.accounts.refresh_all(function(response) {
        if (!api_was_ok(response)) {
            show_api_error(response);
        }
    });
}


////////////////////////////////////////////////////////////////////////////////////////////////////
// CONTEXT MENU

function show_account_contextmenu(event, kind, id, account_id, title) {
    event.preventDefault();
    event.stopPropagation();
    context_target = {
        kind: kind,
        id: id,
        account_id: account_id,
    };
    context_title.textContent = title;
    context_settings_link.href = `/account/${account_id}/settings`;
    contextmenus.show_menu(event, context_menu);
}


////////////////////////////////////////////////////////////////////////////////////////////////////
// STATUS BAR

function status_account_name(account_id) {
    if (account_id === undefined || account_id === null) {
        return "Unknown account";
    }
    const account = find_account(account_id);
    if (account) {
        return account.display_name || account.label || `Account ${account_id}`;
    }
    return `Account ${account_id}`;
}

function status_detail_for(state) {
    if (!state) {
        return "";
    }
    if (state.stage === "folders") {
        return state.total ? `Discovering folders (${state.total})` : "Discovering folders…";
    }
    if (state.stage === "folder_start") {
        return `Folder ${state.index + 1}/${state.total}: ${state.folder_name}`;
    }
    if (state.stage === "headers" || state.stage === "bodies") {
        const label = state.stage === "headers" ? "Headers" : "Messages";
        return `${label}: ${state.folder_name} (${state.done}/${state.total})`;
    }
    if (state.stage === "folder_finished") {
        return `Finished ${state.folder_name}`;
    }
    if (state.stage === "error") {
        return "See account settings for details.";
    }
    if (state.stage === "finished") {
        return "Sync complete.";
    }
    return "Preparing account…";
}

function update_status_timestamp() {
    if (!status_timestamp_element) {
        return;
    }
    status_timestamp_element.textContent = new Date().toLocaleString();
}

function render_status_bar() {
    if (!status_text_element) {
        return;
    }
    update_status_timestamp();
    if (!refresh_status) {
        status_text_element.textContent = "Idle";
        status_detail_element.textContent = "";
        status_spinner.hide();
        return;
    }
    if (refresh_status.error) {
        status_text_element.textContent = `Sync error: ${refresh_status.name}`;
        status_detail_element.textContent = status_detail_for(refresh_status);
        status_spinner.hide();
        return;
    }
    status_text_element.textContent = `Syncing ${refresh_status.name}`;
    status_detail_element.textContent = status_detail_for(refresh_status);
    status_spinner.show();
}

function begin_refresh_status(account) {
    refresh_status = {
        account_id: account.id,
        name: account.display_name || account.label || `Account ${account.id}`,
        stage: "starting",
        folder_name: "",
        done: 0,
        total: 0,
        index: 0,
        error: null,
    };
    render_status_bar();
}

function update_refresh_status(payload) {
    const payload_account_id = payload.account_id;
    if (!refresh_status) {
        const account_id = (
            payload_account_id === undefined ?
            active_account_id :
            payload_account_id
        );
        refresh_status = {
            account_id: account_id,
            name: status_account_name(account_id),
            stage: "starting",
            folder_name: "",
            done: 0,
            total: 0,
            index: 0,
            error: null,
        };
    } else if (
        payload_account_id !== undefined &&
        String(refresh_status.account_id) !== String(payload_account_id)
    ) {
        refresh_status = {
            account_id: payload_account_id,
            name: status_account_name(payload_account_id),
            stage: "starting",
            folder_name: "",
            done: 0,
            total: 0,
            index: 0,
            error: null,
        };
    }
    refresh_status.stage = payload.stage || refresh_status.stage;
    if (payload.folder_name) {
        refresh_status.folder_name = payload.folder_name;
    }
    if (payload.index !== undefined) {
        refresh_status.index = payload.index;
    }
    if (payload.total !== undefined) {
        refresh_status.total = payload.total;
    }
    if (payload.done !== undefined) {
        refresh_status.done = payload.done;
    }
    render_status_bar();
}

function finish_refresh_status(payload) {
    if (
        !refresh_status ||
        String(refresh_status.account_id) !== String(payload.account_id)
    ) {
        refresh_status = {
            account_id: payload.account_id,
            name: status_account_name(payload.account_id),
            stage: "finished",
            folder_name: "",
            done: 0,
            total: 0,
            index: 0,
            error: null,
        };
    }
    refresh_status.error = payload.error || null;
    refresh_status.stage = payload.error ? "error" : "finished";
    render_status_bar();
}

function finish_refresh_queue() {
    if (refresh_status && !refresh_status.error) {
        refresh_status = null;
    }
    render_status_bar();
}


////////////////////////////////////////////////////////////////////////////////////////////////////
// SSE

function start_sse() {
    if (sse !== null) {
        sse.close();
    }
    sse = new EventSource("/sse");

    sse.addEventListener("account_refresh_started", function(event) {
        accounts_spinner.show();
        let payload = null;
        try {
            payload = JSON.parse(event.data);
        } catch (exc) {
            return;
        }
        begin_refresh_status(payload);
    });

    sse.addEventListener("account_refresh_progress", function(event) {
        let payload = null;
        try {
            payload = JSON.parse(event.data);
        } catch (exc) {
            return;
        }
        update_refresh_status(payload);
        if (payload.stage === "folders" || payload.stage === "folder_finished") {
            clearTimeout(accounts_reload_timeout);
            accounts_reload_timeout = setTimeout(load_accounts, 500);
        }
    });

    sse.addEventListener("account_refresh_finished", function(event) {
        const payload = JSON.parse(event.data);
        if (payload.error) {
            console.warn(payload.error);
        }
        finish_refresh_status(payload);
        load_accounts();
    });

    sse.addEventListener("account_refresh_queue_finished", function() {
        accounts_spinner.hide();
        finish_refresh_queue();
        load_accounts(function() {
            if (refresh_reload_pending) {
                refresh_reload_pending = false;
                if (active_kind !== null) {
                    load_messages(true);
                }
            }
        });
    });

    sse.addEventListener("keepalive", function() {
        clearTimeout(sse_watchdog_timeout);
        sse_watchdog_timeout = setTimeout(start_sse, 120000);
    });
}


////////////////////////////////////////////////////////////////////////////////////////////////////
// PAGE LOAD

function figure_initial_selection() {
    if (window.location.pathname === "/all") {
        select_all_accounts();
        return;
    }
    const folder_match = window.location.pathname.match(/^\/folder\/(\d+)/);
    const account_match = window.location.pathname.match(/^\/account\/(\d+)/);

    if (folder_match) {
        const folder = find_folder(folder_match[1]);
        if (folder) {
            select_folder(folder.id);
            return;
        }
    }
    if (account_match) {
        const account = find_account(account_match[1]);
        if (account) {
            select_account(account.id);
            return;
        }
    }

    const first_account = accounts_data[0];
    if (!first_account) {
        set_active_title("No accounts yet. Use +Account to add one.");
        return;
    }
    const inbox = first_account.folders.find(folder => folder.special_use === "\\Inbox");
    const first_folder = inbox || first_account.folders.find(folder => folder.selectable);
    if (first_folder) {
        select_folder(first_folder.id);
    } else {
        select_account(first_account.id);
    }
}

function on_pageload() {
    accounts_element = document.getElementById("account_list");
    messages_element = document.getElementById("message_list");
    filter_input = document.getElementById("filter_input");
    active_title_element = document.getElementById("active_title");

    accounts_spinner = new spinners.Spinner(document.getElementById("accounts_loading_spinner"));
    news_spinner = new spinners.Spinner(document.getElementById("news_loading_spinner"));
    reader_spinner = new spinners.Spinner(document.getElementById("reader_loading_spinner"));
    status_spinner = new spinners.Spinner(document.getElementById("status_spinner"));
    status_text_element = document.getElementById("status_text");
    status_detail_element = document.getElementById("status_detail");
    status_timestamp_element = document.getElementById("status_timestamp");
    update_status_timestamp();

    reader_subject = document.getElementById("reader_subject");
    reader_meta = document.getElementById("reader_meta");
    reader_attachments = document.getElementById("reader_attachments");
    reader_iframe = document.getElementById("reader_iframe");
    reader_plain = document.getElementById("reader_plain");
    reader_headers = document.getElementById("reader_headers");
    reader_body = document.getElementById("reader_body");
    reader_html_button = document.getElementById("reader_html_button");
    reader_text_button = document.getElementById("reader_text_button");
    reader_images_button = document.getElementById("reader_images_button");
    reader_headers_button = document.getElementById("reader_headers_button");

    context_menu = document.getElementById("mail_contextmenu");
    context_title = document.getElementById("context_title");
    context_refresh_button = document.getElementById("context_refresh_button");
    context_settings_link = document.getElementById("context_settings_link");

    messages_element.addEventListener("click", on_messages_click);
    messages_element.addEventListener("scroll", maybe_load_more_messages);

    document.getElementById("refresh_all_button").addEventListener("click", refresh_all);
    document.getElementById("refresh_folder_button").addEventListener("click", refresh_active);
    document.getElementById("mark_read_button").addEventListener("click", function() { mark_selected(true); });
    document.getElementById("mark_unread_button").addEventListener("click", function() { mark_selected(false); });
    document.getElementById("delete_button").addEventListener("click", delete_selected);
    document.getElementById("move_select").addEventListener("change", move_selected);

    filter_input.addEventListener("input", function() {
        clearTimeout(dynamic_filter_timeout);
        dynamic_filter_timeout = setTimeout(apply_filter, 200);
    });

    reader_html_button.addEventListener("click", function() {
        reader_mode = "html";
        reader_show_headers = false;
        render_reader_body();
    });
    reader_text_button.addEventListener("click", function() {
        reader_mode = "text";
        reader_show_headers = false;
        render_reader_body();
    });
    reader_images_button.addEventListener("click", function() {
        allow_images = true;
        render_reader_body();
    });
    reader_headers_button.addEventListener("click", toggle_headers);
    reader_iframe.addEventListener("load", resize_reader_iframe);

    context_refresh_button.addEventListener("click", function() {
        contextmenus.hide_open_menus();
        if (!context_target) {
            return;
        }
        if (context_target.kind === "folder") {
            api.folders.refresh(context_target.id, function(response) {
                if (!api_was_ok(response)) {
                    show_api_error(response);
                }
            });
        } else {
            refresh_account(context_target.account_id);
        }
    });

    accounts_element.addEventListener("contextmenu", function(event) {
        const folder = event.target.closest(".folder");
        if (folder) {
            show_account_contextmenu(
                event,
                "folder",
                folder.dataset.folderId,
                folder.dataset.accountId,
                folder.querySelector(".title").textContent
            );
            return;
        }
        const header = event.target.closest(".account_header");
        if (header) {
            const account = find_account(header.dataset.accountId);
            if (account) {
                show_account_contextmenu(
                    event,
                    "account",
                    account.id,
                    account.id,
                    account.display_name
                );
            }
        }
    });

    hotkeys.register_hotkey("ctrl a", select_all_visible, "Select all visible messages.");
    hotkeys.register_hotkey("arrowup", function(event) { navigate(-1); }, "Previous message.");
    hotkeys.register_hotkey("arrowdown", function(event) { navigate(1); }, "Next message.");
    hotkeys.register_hotkey("delete", function(event) { delete_selected(); }, "Delete selected messages.");
    hotkeys.register_help("Click a message to select it. Ctrl-click toggles; Shift-click selects a range.");
    hotkeys.register_help("Right-click an account or folder for refresh and settings.");

    load_accounts(function() {
        figure_initial_selection();
    });
    start_sse();
}

document.addEventListener("DOMContentLoaded", on_pageload);
