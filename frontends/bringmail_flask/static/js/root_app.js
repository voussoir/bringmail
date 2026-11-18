"use strict";

const BLOCKED_IMAGE = "data:image/svg+xml;charset=utf-8," + encodeURIComponent(
    '<svg xmlns="http://www.w3.org/2000/svg" width="240" height="32">' +
    '<rect width="100%" height="100%" fill="#eeeeee"/>' +
    '<text x="8" y="21" font-family="sans-serif" font-size="12" fill="#666666">Remote image blocked</text>' +
    '</svg>'
);

const MESSAGE_PAGE_SIZE = 500;

// Browser tab title when every account is read; when any account has unread
// messages the total across all accounts is appended in parentheses.
const BASE_TAB_TITLE = "BringMail";

// Clicking the already-selected account or folder reloads the message list,
// but a rapid second click (for example the second half of a double-click)
// must not trigger a second request.
const RESELECTION_NOOP_MS = 500;

let accounts_element = null;
let messages_element = null;
let filter_input = null;
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
let context_retry_pending_button = null;
let context_discard_pending_button = null;
let context_settings_link = null;
let context_target = null;

let message_context_menu = null;
let message_context_title = null;
let message_context_read_button = null;
let message_context_unread_button = null;
let message_context_delete_button = null;
let message_context_restore_button = null;
let message_context_move_list = null;
let message_context_ids = [];

let drag_message_ids = [];
let drag_source_account_ids = new Set();

// Reordering the account tree uses the same panel-level drag events as moving
// messages into folders. drag_account_id is non-null only while an account
// header is being dragged, which keeps the two drags separate.
let drag_account_id = null;
let account_drop_target = null;
let account_drop_before = false;
let suppress_account_click_until = 0;

let accounts_data = [];
let active_kind = null;
let active_id = null;
let active_account_id = null;
let active_folder_id = null;
let last_selection_key = null;
let last_selection_time = 0;

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

function update_tab_title(total_unread) {
    const count = Math.max(0, total_unread || 0);
    document.title = count > 0 ? `${BASE_TAB_TITLE} (${count})` : BASE_TAB_TITLE;
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
    // A background refresh can rebuild this tree in the middle of a drag. Drop
    // the drag state so the new rows are not tied to detached elements.
    clear_account_drag_state();
    common.delete_all_children(accounts_element);

    const all_div = document.createElement("div");
    all_div.classList.add("account", "all_accounts");
    const all_header = document.createElement("div");
    all_header.classList.add("account_header", "all_accounts_header");
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
        header.draggable = true;

        const toggle = document.createElement("button");
        toggle.type = "button";
        toggle.classList.add("collapse_toggle");
        toggle.addEventListener("click", function(event) {
            event.stopPropagation();
            toggle_account_collapsed(account_div, account.id);
        });
        toggle.addEventListener("dblclick", function(event) {
            event.stopPropagation();
        });
        header.appendChild(toggle);

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

        const pending = document.createElement("span");
        pending.classList.add("pending_count");
        const pending_total = (account.pending_count || 0) + (account.conflict_count || 0);
        pending.classList.toggle("hidden", pending_total === 0);
        if (pending_total > 0) {
            if (account.conflict_count) {
                pending.classList.add("pending_conflict");
                pending.textContent = `!${account.conflict_count}`;
                pending.title = `${account.conflict_count} action(s) could not be synced. Right-click the account for queue actions.`;
            } else {
                pending.classList.add("pending_waiting");
                pending.textContent = `↑${account.pending_count}`;
                pending.title = `${account.pending_count} action(s) waiting to sync. Right-click the account for queue actions.`;
            }
        }
        header.appendChild(pending);

        header.addEventListener("click", function() {
            // The click that can follow a drag that never left its origin must
            // not move the selection to the account the user just grabbed.
            if (Date.now() < suppress_account_click_until) {
                return;
            }
            select_account(account.id);
        });
        header.addEventListener("dblclick", function(event) {
            event.stopPropagation();
            toggle_account_collapsed(account_div, account.id);
        });
        header.addEventListener("contextmenu", function(event) {
            show_account_contextmenu(event, "account", account.id, account.id, account.display_name);
        });
        header.addEventListener("dragstart", on_account_dragstart);
        header.addEventListener("dragend", on_account_dragend);

        account_div.appendChild(header);
        update_collapse_toggle(account_div);

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
}

function retry_pending_actions(account_id) {
    api.accounts.flush(account_id, function(response) {
        if (!api_was_ok(response)) {
            show_api_error(response);
        }
    });
}

function discard_pending_actions(account_id) {
    const account = find_account(account_id);
    if (!account) {
        return;
    }
    const total = (account.pending_count || 0) + (account.conflict_count || 0);
    if (total === 0) {
        return;
    }
    const name = account.display_name || account.label || `Account ${account_id}`;
    const message = (
        `Discard ${total} pending action(s) for ${name}? ` +
        `The local changes will be reverted and the actions will not be sent.`
    );
    if (!window.confirm(message)) {
        return;
    }
    api.pending.discard_all(account_id, function(response) {
        if (!api_was_ok(response)) {
            show_api_error(response);
        }
    });
}

function update_collapse_toggle(account_div) {
    const toggle = account_div.querySelector(".collapse_toggle");
    if (!toggle) {
        return;
    }
    const collapsed = account_div.classList.contains("collapsed");
    toggle.textContent = collapsed ? "▸" : "▾";
    toggle.title = collapsed ? "Expand account" : "Collapse account";
    toggle.setAttribute("aria-expanded", String(!collapsed));
}

function toggle_account_collapsed(account_div, account_id) {
    if (account_div.classList.contains("collapsed")) {
        account_div.classList.remove("collapsed");
        localStorage.removeItem(`bringmail_collapse_${account_id}`);
    } else {
        account_div.classList.add("collapsed");
        localStorage.setItem(`bringmail_collapse_${account_id}`, "1");
    }
    update_collapse_toggle(account_div);
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
    update_tab_title(total_unread);
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

function is_duplicate_selection(kind, id) {
    const key = `${kind}:${id}`;
    const now = Date.now();
    const is_active_view = (
        kind === active_kind &&
        String(id) === String(active_id)
    );
    const is_duplicate = (
        is_active_view &&
        key === last_selection_key &&
        (now - last_selection_time) < RESELECTION_NOOP_MS
    );
    last_selection_key = key;
    last_selection_time = now;
    return is_duplicate;
}

function select_all_accounts() {
    if (is_duplicate_selection("all", "all")) {
        return;
    }
    refresh_reload_pending = false;
    active_kind = "all";
    active_id = "all";
    active_account_id = null;
    active_folder_id = null;
    history.replaceState(null, "", "/all");

    update_active_classes();
    load_messages(true);
}

function select_account(account_id) {
    const account = find_account(account_id);
    if (!account) {
        return;
    }
    if (is_duplicate_selection("account", account.id)) {
        return;
    }
    refresh_reload_pending = false;
    active_kind = "account";
    active_id = account.id;
    active_account_id = account.id;
    active_folder_id = null;
    history.replaceState(null, "", `/account/${account.id}`);

    update_active_classes();
    load_messages(true);
}

function select_folder(folder_id) {
    const folder = find_folder(folder_id);
    if (!folder) {
        return;
    }
    if (!folder.selectable) {
        return;
    }
    if (is_duplicate_selection("folder", folder.id)) {
        return;
    }
    refresh_reload_pending = false;
    active_kind = "folder";
    active_id = folder.id;
    active_account_id = folder.account_id;
    active_folder_id = folder.id;
    history.replaceState(null, "", `/folder/${folder.id}`);

    const account_div = accounts_element.querySelector(`.account[data-account-id="${folder.account_id}"]`);
    if (account_div && account_div.classList.contains("collapsed")) {
        toggle_account_collapsed(account_div, folder.account_id);
    }

    update_active_classes();
    load_messages(true);
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

// Messages arrive newest first, so the messages of one calendar day are
// contiguous. A separator is emitted only for the first message of each day.
function message_day_key(message) {
    if (message.date_unix === null || message.date_unix === undefined) {
        return null;
    }
    const date = new Date(message.date_unix * 1000);
    return `${date.getFullYear()}-${date.getMonth()}-${date.getDate()}`;
}

function create_date_header(message) {
    const header = document.createElement("div");
    header.classList.add("date_header");
    header.textContent = new Date(message.date_unix * 1000).toLocaleDateString(
        undefined,
        {
            weekday: "short",
            year: "numeric",
            month: "short",
            day: "numeric",
        }
    );
    return header;
}

function create_message_row(message) {
    const row = document.createElement("div");
    row.classList.add("message");
    row.dataset.id = message.id;
    row.dataset.folderId = message.folder_id;
    row.dataset.accountId = message.account_id;
    row.dataset.date = message.date_unix;
    row.draggable = true;
    if (!message.seen) {
        row.classList.add("unread");
    }
    if (trashed_ids.has(String(message.id)) || message.local_deleted) {
        row.classList.add("trashed");
    }
    if (message.pending) {
        row.classList.add("pending");
        if (message.pending.state === "conflicted" || message.pending.state === "failed") {
            row.classList.add("pending_conflict");
        }
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

    const pending = document.createElement("span");
    pending.classList.add("pending_badge");
    if (message.pending) {
        const conflicted = (
            message.pending.state === "conflicted" ||
            message.pending.state === "failed"
        );
        pending.textContent = conflicted ? "!" : "↑";
        pending.title = conflicted
            ? `${message.pending.label || "Action"} could not be synced (${message.pending.state}).`
            : `${message.pending.label || "Action"} waiting to sync.`;
        if (conflicted) {
            pending.classList.add("pending_conflict");
        }
    }
    row.appendChild(pending);

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
    // When more messages are appended, the first new row must be compared
    // against the last row that is already on screen so a day that straddles
    // two pages does not get a second separator.
    let previous_day = null;
    if (messages_rendered > 0) {
        previous_day = message_day_key(messages_data[messages_rendered - 1]);
    }
    for (let index = messages_rendered; index < messages_data.length; index += 1) {
        const message = messages_data[index];
        const day = message_day_key(message);
        if (day !== null && day !== previous_day) {
            fragment.appendChild(create_date_header(message));
        }
        previous_day = day;
        fragment.appendChild(create_message_row(message));
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

function set_message_pending_local(message_id, pending) {
    message_id = String(message_id);
    const message = find_message(message_id);
    if (message) {
        message.pending = pending || null;
    }
    const row = message_row(message_id);
    if (!row) {
        return;
    }
    const badge = row.querySelector(".pending_badge");
    if (!pending) {
        row.classList.remove("pending", "pending_conflict");
        if (badge) {
            badge.textContent = "";
            badge.title = "";
        }
        return;
    }
    const conflicted = (
        pending.state === "conflicted" ||
        pending.state === "failed"
    );
    row.classList.add("pending");
    row.classList.toggle("pending_conflict", conflicted);
    if (badge) {
        badge.textContent = conflicted ? "!" : "↑";
        badge.title = conflicted
            ? `${pending.label || "Action"} could not be synced (${pending.state}).`
            : `${pending.label || "Action"} waiting to sync.`;
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
    update_date_headers();
}

function update_date_headers() {
    // A separator is shown only while at least one message below it and above
    // the next separator is visible. A separator whose messages were all
    // removed is dropped from the DOM instead of lingering unseen.
    let header = null;
    let has_rows = false;
    let has_visible_rows = false;

    function finish_group() {
        if (header === null) {
            return;
        }
        if (!has_rows) {
            header.remove();
        } else {
            header.classList.toggle("hidden", !has_visible_rows);
        }
    }

    for (const child of Array.from(messages_element.children)) {
        if (child.classList.contains("date_header")) {
            finish_group();
            header = child;
            has_rows = false;
            has_visible_rows = false;
            continue;
        }
        if (!child.classList.contains("message")) {
            continue;
        }
        has_rows = true;
        if (!child.classList.contains("hidden")) {
            has_visible_rows = true;
        }
    }
    finish_group();
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

function mark_selected(seen, ids) {
    if (!ids) {
        ids = selected_or_current_ids();
    }
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

function move_messages_to_account(ids, folder_id) {
    api.messages.batch_move_to_account(ids, folder_id, function(response) {
        batch_result_handler(response, function(data) {
            const entries = (data && data.messages) ? data.messages : [];
            const to_remove = [];
            for (const entry of entries) {
                const message_id = String(entry.id);
                if (entry.copy_only) {
                    // The source has no usable Trash folder, or the message is
                    // already in Trash. The copy is queued but the source row
                    // must stay where it is.
                    set_message_pending_local(message_id, entry.pending);
                } else {
                    to_remove.push(message_id);
                }
            }
            if (to_remove.length) {
                clear_reader();
                remove_messages_locally(to_remove);
            }
            load_accounts();
        });
    });
}

function move_messages(ids, folder_id) {
    if (!folder_id || !ids || ids.length === 0) {
        return;
    }

    const target_folder = find_folder(folder_id);
    if (!target_folder) {
        return;
    }
    const messages = ids.map(find_message).filter(Boolean);
    if (messages.length === 0) {
        return;
    }
    const source_account_ids = new Set(
        messages.map(message => String(message.account_id))
    );
    const cross_account = Array.from(source_account_ids).some(
        account_id => account_id !== String(target_folder.account_id)
    );
    if (cross_account) {
        if (source_account_ids.size !== 1) {
            alert("Move requires messages from one account.");
            return;
        }
        move_messages_to_account(ids, folder_id);
        return;
    }

    const previously_trashed = new Set(
        ids.map(String).filter(message_id => trashed_ids.has(String(message_id)))
    );
    const target_is_trash = target_folder.special_use === "\\Trash";
    const target_is_active = (
        active_kind === "folder" &&
        String(folder_id) === String(active_folder_id)
    );

    api.messages.batch_move(ids, folder_id, function(response) {
        batch_result_handler(response, function(returned_ids) {
            const to_remove = [];
            for (const message_id of returned_ids) {
                const message = find_message(message_id);
                if (
                    message &&
                    String(message.folder_id) === String(folder_id) &&
                    !previously_trashed.has(String(message_id))
                ) {
                    // Already in the destination folder; the queue treated
                    // this as a no-op.
                    continue;
                }
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
            load_accounts();
        });
    });
}

function delete_messages(ids) {
    if (!ids || ids.length === 0) {
        return;
    }
    // Show the messages as trashed immediately and queue the delete. The
    // rows stay in place with a strikethrough; if the delete is still
    // pending the user can undo it, and once it is flushed the next reload
    // of this view drops the rows because they now belong to the trash.
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
            show_api_error(response);
            return;
        }
        load_accounts();
    });
}

function delete_selected() {
    delete_messages(selected_or_current_ids());
}

function restore_messages(ids) {
    if (!ids || ids.length === 0) {
        return;
    }
    api.messages.batch_restore(ids, function(response) {
        if (!api_was_ok(response)) {
            show_api_error(response);
            return;
        }
        for (const message_id of ids) {
            const message = find_message(message_id);
            const folder_id = message ? message.folder_id : null;
            untrash_local(message_id, folder_id);
            if (message) {
                message.local_deleted = false;
            }
        }
        load_accounts();
        if (active_kind !== null) {
            load_messages(true);
        }
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
    const account = find_account(account_id);
    const pending_count = account ? (account.pending_count || 0) : 0;
    const conflict_count = account ? (account.conflict_count || 0) : 0;
    if (context_retry_pending_button) {
        context_retry_pending_button.classList.toggle("hidden", pending_count === 0);
    }
    if (context_discard_pending_button) {
        context_discard_pending_button.classList.toggle(
            "hidden",
            (pending_count + conflict_count) === 0
        );
    }
    context_title.textContent = title;
    context_settings_link.href = `/account/${account_id}/settings`;
    contextmenus.show_menu(event, context_menu);
}


////////////////////////////////////////////////////////////////////////////////////////////////////
// MESSAGE CONTEXT MENU

function on_messages_contextmenu(event) {
    const row = event.target.closest(".message");
    if (!row) {
        return;
    }
    event.preventDefault();
    event.stopPropagation();

    const message_id = String(row.dataset.id);
    if (!selected_ids.has(message_id)) {
        select_only(message_id);
    }
    const ids = get_selected_ids();
    if (ids.length === 0) {
        return;
    }
    build_message_contextmenu(ids);
    contextmenus.show_menu(event, message_context_menu);
}

function build_message_contextmenu(ids) {
    message_context_ids = ids;
    if (ids.length === 1) {
        const message = find_message(ids[0]);
        message_context_title.textContent = message ? (message.subject || "(no subject)") : "Message";
    } else {
        message_context_title.textContent = `${ids.length} messages`;
    }

    const can_restore = ids.some(function(message_id) {
        const message = find_message(message_id);
        const pending_delete = Boolean(
            message &&
            message.pending &&
            message.pending.operation === "delete"
        );
        return Boolean(
            trashed_ids.has(String(message_id)) ||
            (message && (message.local_deleted || pending_delete))
        );
    });
    message_context_restore_button.classList.toggle("hidden", !can_restore);

    common.delete_all_children(message_context_move_list);
    const destinations = message_context_destinations(ids);
    if (destinations.note) {
        const note = document.createElement("div");
        note.classList.add("context_note");
        note.textContent = destinations.note;
        message_context_move_list.appendChild(note);
    }
    let last_account_id = null;
    for (const destination of destinations.folders) {
        const account = destination.account;
        const folder = destination.folder;
        if (String(account.id) !== String(last_account_id)) {
            const heading = document.createElement("div");
            heading.classList.add("context_note");
            heading.textContent = account.display_name;
            message_context_move_list.appendChild(heading);
            last_account_id = account.id;
        }
        const button = document.createElement("button");
        button.type = "button";
        button.textContent = folder.display_name || folder.name;
        button.title = `${account.display_name}: ${folder.name}`;
        button.addEventListener("click", function() {
            contextmenus.hide_open_menus();
            move_messages(ids, folder.id);
        });
        message_context_move_list.appendChild(button);
    }
}

function message_context_destinations(ids) {
    const messages = ids.map(find_message).filter(Boolean);
    if (messages.length === 0) {
        return {folders: [], note: "Move is unavailable."};
    }
    const source_account_id = messages[0].account_id;
    if (!messages.every(message => String(message.account_id) === String(source_account_id))) {
        return {folders: [], note: "Move requires messages from one account."};
    }
    const folder_ids = new Set(messages.map(message => String(message.folder_id)));
    const current_folder_id = folder_ids.size === 1 ? Array.from(folder_ids)[0] : null;
    const folders = [];
    for (const account of accounts_data) {
        for (const folder of account.folders) {
            if (!folder.selectable) {
                continue;
            }
            if (current_folder_id !== null && String(folder.id) === current_folder_id) {
                continue;
            }
            folders.push({account: account, folder: folder});
        }
    }
    return {
        folders: folders,
        note: folders.length === 0 ? "No other folders." : "",
    };
}


////////////////////////////////////////////////////////////////////////////////////////////////////
// DRAG AND DROP

function clear_drop_targets() {
    if (!accounts_element) {
        return;
    }
    for (const folder_element of accounts_element.querySelectorAll(".folder.drop_target")) {
        folder_element.classList.remove("drop_target");
    }
}

function clear_drag_state() {
    if (messages_element) {
        for (const row of messages_element.querySelectorAll(".message.dragging")) {
            row.classList.remove("dragging");
        }
    }
    clear_drop_targets();
    drag_message_ids = [];
    drag_source_account_ids = new Set();
}

function account_row(element) {
    if (!element || !element.closest) {
        return null;
    }
    const header = element.closest(".account_header");
    if (!header || header.dataset.allAccounts) {
        return null;
    }
    return header.closest(".account");
}

function clear_account_drop_target() {
    if (!accounts_element || account_drop_target === null) {
        return;
    }
    for (const account_div of accounts_element.querySelectorAll(".account.drop_before, .account.drop_after")) {
        account_div.classList.remove("drop_before", "drop_after");
    }
    account_drop_target = null;
}

function clear_account_drag_state() {
    if (accounts_element) {
        for (const account_div of accounts_element.querySelectorAll(".account.dragging")) {
            account_div.classList.remove("dragging");
        }
    }
    clear_account_drop_target();
    drag_account_id = null;
}

function on_account_dragstart(event) {
    const account_div = account_row(event.target);
    if (!account_div || !event.dataTransfer) {
        event.preventDefault();
        return;
    }
    drag_account_id = String(account_div.dataset.accountId);
    event.dataTransfer.effectAllowed = "move";
    // Firefox only starts a drag when some data is set. The custom type lets
    // the panel distinguish an account drag from a message drag.
    event.dataTransfer.setData("application/x-bringmail-account", drag_account_id);
    event.dataTransfer.setData("text/plain", drag_account_id);
    account_div.classList.add("dragging");
}

function on_account_dragend(event) {
    suppress_account_click_until = Date.now() + 200;
    clear_account_drag_state();
}

function on_account_reorder_dragover(event) {
    const account_div = account_row(event.target);
    if (
        !account_div ||
        !event.dataTransfer ||
        String(account_div.dataset.accountId) === drag_account_id
    ) {
        clear_account_drop_target();
        return;
    }
    event.preventDefault();
    event.dataTransfer.dropEffect = "move";

    const header = account_div.querySelector(".account_header");
    const rect = header.getBoundingClientRect();
    const before = event.clientY < (rect.top + (rect.height / 2));
    if (account_drop_target === account_div && account_drop_before === before) {
        return;
    }
    clear_account_drop_target();
    account_drop_target = account_div;
    account_drop_before = before;
    account_div.classList.add(before ? "drop_before" : "drop_after");
}

function ordered_account_ids_after_drop(dragged_id, target_id, before) {
    const account_ids = accounts_data.map(account => String(account.id));
    const from_index = account_ids.indexOf(String(dragged_id));
    if (from_index === -1) {
        return null;
    }
    account_ids.splice(from_index, 1);
    let to_index = account_ids.indexOf(String(target_id));
    if (to_index === -1) {
        return null;
    }
    if (!before) {
        to_index += 1;
    }
    account_ids.splice(to_index, 0, String(dragged_id));
    return account_ids;
}

function apply_account_order(account_ids) {
    const by_id = new Map(accounts_data.map(account => [String(account.id), account]));
    const ordered = [];
    for (const account_id of account_ids) {
        const account = by_id.get(String(account_id));
        if (!account) {
            return false;
        }
        ordered.push(account);
    }
    if (ordered.length !== accounts_data.length) {
        return false;
    }
    accounts_data = ordered;
    render_accounts();
    return true;
}

function on_account_reorder_drop(event) {
    const dragged_id = drag_account_id;
    const target_div = account_drop_target;
    const before = account_drop_before;
    clear_account_drag_state();

    if (
        dragged_id === null ||
        target_div === null ||
        String(target_div.dataset.accountId) === dragged_id
    ) {
        return;
    }
    event.preventDefault();

    const account_ids = ordered_account_ids_after_drop(
        dragged_id,
        target_div.dataset.accountId,
        before
    );
    if (!account_ids || !apply_account_order(account_ids)) {
        return;
    }

    api.accounts.reorder(account_ids, function(response) {
        if (!api_was_ok(response)) {
            show_api_error(response);
            load_accounts();
            return;
        }
        accounts_data = response.data;
        render_accounts();
    });
}

function on_messages_dragstart(event) {
    const row = event.target.closest(".message");
    if (!row || !event.dataTransfer) {
        event.preventDefault();
        return;
    }

    const message_id = String(row.dataset.id);
    if (!selected_ids.has(message_id)) {
        select_only(message_id);
    }
    const ids = get_selected_ids();
    if (ids.length === 0) {
        event.preventDefault();
        return;
    }

    const messages = ids.map(find_message).filter(Boolean);
    const account_ids = new Set(
        messages.map(message => String(message.account_id))
    );
    if (account_ids.size !== 1) {
        event.preventDefault();
        alert("Move requires messages from one account.");
        return;
    }

    drag_message_ids = ids;
    drag_source_account_ids = account_ids;
    event.dataTransfer.effectAllowed = "move";
    event.dataTransfer.setData("text/plain", ids.join(","));
    for (const selected_id of ids) {
        const selected_row = message_row(selected_id);
        if (selected_row) {
            selected_row.classList.add("dragging");
        }
    }
}

function on_messages_dragend(event) {
    clear_drag_state();
}

function on_folder_dragover(event) {
    if (drag_message_ids.length === 0 || drag_source_account_ids.size !== 1) {
        return;
    }
    const folder_element = event.target.closest(".folder");
    if (!folder_element) {
        return;
    }
    const folder = find_folder(folder_element.dataset.folderId);
    if (!folder || !folder.selectable || !event.dataTransfer) {
        return;
    }
    event.preventDefault();
    event.dataTransfer.dropEffect = "move";
    for (const other of accounts_element.querySelectorAll(".folder.drop_target")) {
        if (other !== folder_element) {
            other.classList.remove("drop_target");
        }
    }
    folder_element.classList.add("drop_target");
}

function on_folder_dragleave(event) {
    const folder_element = event.target.closest(".folder");
    if (!folder_element) {
        return;
    }
    if (event.relatedTarget && folder_element.contains(event.relatedTarget)) {
        return;
    }
    folder_element.classList.remove("drop_target");
}

function on_folder_drop(event) {
    const folder_element = event.target.closest(".folder");
    if (
        !folder_element ||
        drag_message_ids.length === 0 ||
        drag_source_account_ids.size !== 1
    ) {
        return;
    }
    event.preventDefault();
    const folder = find_folder(folder_element.dataset.folderId);
    const ids = drag_message_ids.slice();
    clear_drag_state();
    if (!folder || !folder.selectable) {
        return;
    }
    move_messages(ids, folder.id);
}

function on_accounts_dragover(event) {
    if (drag_account_id !== null) {
        on_account_reorder_dragover(event);
        return;
    }
    on_folder_dragover(event);
}

function on_accounts_dragleave(event) {
    if (drag_account_id !== null) {
        if (event.relatedTarget && accounts_element.contains(event.relatedTarget)) {
            return;
        }
        clear_account_drop_target();
        return;
    }
    on_folder_dragleave(event);
}

function on_accounts_drop(event) {
    if (drag_account_id !== null) {
        on_account_reorder_drop(event);
        return;
    }
    on_folder_drop(event);
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
    if (state.stage === "outbox_start") {
        return "Sending pending actions…";
    }
    if (state.stage === "outbox_progress") {
        return `Sending pending actions (${state.done}/${state.total})`;
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
    const verb = String(refresh_status.stage).startsWith("outbox") ? "Sending" : "Syncing";
    status_text_element.textContent = `${verb} ${refresh_status.name}`;
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
    // UI contract: background refreshes, including scheduled syncs and IMAP
    // IDLE notifications, must never auto-reload the message list or the
    // reader panel. Only the account tree and unread counts are refreshed;
    // the user re-clicks the account or folder to load new messages.
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

    sse.addEventListener("folder_sync_started", function(event) {
        let payload = null;
        try {
            payload = JSON.parse(event.data);
        } catch (exc) {
            return;
        }
        begin_refresh_status({
            id: payload.account_id,
            display_name: payload.account_name,
        });
        update_refresh_status(Object.assign(
            {},
            payload,
            {stage: "folder_start", index: 0, total: 1}
        ));
    });

    sse.addEventListener("folder_sync_progress", function(event) {
        let payload = null;
        try {
            payload = JSON.parse(event.data);
        } catch (exc) {
            return;
        }
        update_refresh_status(payload);
    });

    sse.addEventListener("folder_sync_finished", function(event) {
        const payload = JSON.parse(event.data);
        if (payload.error) {
            console.warn(payload.error);
        }
        finish_refresh_status(payload);
        load_accounts();
    });

    sse.addEventListener("outbox_started", function(event) {
        let payload = null;
        try {
            payload = JSON.parse(event.data);
        } catch (exc) {
            return;
        }
        refresh_status = {
            account_id: payload.account_id,
            name: status_account_name(payload.account_id),
            stage: "outbox_start",
            folder_name: "",
            done: 0,
            total: 0,
            index: 0,
            error: null,
        };
        render_status_bar();
    });

    sse.addEventListener("outbox_progress", function(event) {
        let payload = null;
        try {
            payload = JSON.parse(event.data);
        } catch (exc) {
            return;
        }
        update_refresh_status(payload);
    });

    sse.addEventListener("outbox_finished", function(event) {
        let payload = null;
        try {
            payload = JSON.parse(event.data);
        } catch (exc) {
            return;
        }
        if (payload.error) {
            console.warn(payload.error);
        }
        finish_refresh_status(payload);
        load_accounts();
        // Deliberately do not reload the message list or the reader here.
        // Flushing is a background operation, like a scheduled refresh. The
        // user's selection, scroll position, and open message must survive a
        // queued read/move/delete being sent to the server.
    });

    sse.addEventListener("pending_discarded", function(event) {
        let payload = null;
        try {
            payload = JSON.parse(event.data);
        } catch (exc) {
            return;
        }
        if (payload.error) {
            console.warn(payload.error);
        }
        load_accounts(function() {
            const affects_active_view = (
                active_kind === "all" ||
                (
                    active_account_id !== null &&
                    String(active_account_id) === String(payload.account_id)
                )
            );
            if (affects_active_view) {
                load_messages(true);
            }
        });
    });

    sse.addEventListener("idle_state", function(event) {
        let payload = null;
        try {
            payload = JSON.parse(event.data);
        } catch (exc) {
            return;
        }
        if (payload.state === "retrying" || payload.state === "unsupported") {
            console.warn(
                `IMAP IDLE for ${payload.account_name || payload.account_id}:`,
                payload.state,
                payload.detail || ""
            );
        }
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

    select_all_accounts();
}

function on_pageload() {
    accounts_element = document.getElementById("account_list");
    messages_element = document.getElementById("message_list");
    filter_input = document.getElementById("filter_input");

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
    context_retry_pending_button = document.getElementById("context_retry_pending_button");
    context_discard_pending_button = document.getElementById("context_discard_pending_button");
    context_settings_link = document.getElementById("context_settings_link");
    if (!context_retry_pending_button || !context_discard_pending_button) {
        console.warn(
            "The page is missing the pending-queue context menu controls. " +
            "Restart the BringMail server and refresh the page to load the current template."
        );
    }

    message_context_menu = document.getElementById("message_contextmenu");
    message_context_title = document.getElementById("message_context_title");
    message_context_read_button = document.getElementById("message_context_read_button");
    message_context_unread_button = document.getElementById("message_context_unread_button");
    message_context_delete_button = document.getElementById("message_context_delete_button");
    message_context_restore_button = document.getElementById("message_context_restore_button");
    message_context_move_list = document.getElementById("message_context_move_list");

    messages_element.addEventListener("click", on_messages_click);
    messages_element.addEventListener("contextmenu", on_messages_contextmenu);
    messages_element.addEventListener("scroll", maybe_load_more_messages);
    messages_element.addEventListener("dragstart", on_messages_dragstart);
    messages_element.addEventListener("dragend", on_messages_dragend);
    accounts_element.addEventListener("dragover", on_accounts_dragover);
    accounts_element.addEventListener("dragleave", on_accounts_dragleave);
    accounts_element.addEventListener("drop", on_accounts_drop);

    message_context_read_button.addEventListener("click", function() {
        contextmenus.hide_open_menus();
        mark_selected(true, message_context_ids);
    });
    message_context_unread_button.addEventListener("click", function() {
        contextmenus.hide_open_menus();
        mark_selected(false, message_context_ids);
    });
    message_context_delete_button.addEventListener("click", function() {
        contextmenus.hide_open_menus();
        delete_messages(message_context_ids);
    });
    message_context_restore_button.addEventListener("click", function() {
        contextmenus.hide_open_menus();
        restore_messages(message_context_ids);
    });

    document.getElementById("refresh_all_button").addEventListener("click", refresh_all);
    document.getElementById("refresh_folder_button").addEventListener("click", refresh_active);
    document.getElementById("mark_read_button").addEventListener("click", function() { mark_selected(true); });
    document.getElementById("mark_unread_button").addEventListener("click", function() { mark_selected(false); });
    document.getElementById("delete_button").addEventListener("click", delete_selected);

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

    if (context_retry_pending_button) {
        context_retry_pending_button.addEventListener("click", function() {
            contextmenus.hide_open_menus();
            if (!context_target) {
                return;
            }
            retry_pending_actions(context_target.account_id);
        });
    }

    if (context_discard_pending_button) {
        context_discard_pending_button.addEventListener("click", function() {
            contextmenus.hide_open_menus();
            if (!context_target) {
                return;
            }
            discard_pending_actions(context_target.account_id);
        });
    }

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
    hotkeys.register_help("Right-click a message for read, delete, and move actions.");
    hotkeys.register_help("Right-click an account or folder for refresh and settings.");

    load_accounts(function() {
        figure_initial_selection();
    });
    start_sse();
}

document.addEventListener("DOMContentLoaded", on_pageload);
