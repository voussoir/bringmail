function get_account_id() {
    const value = document.body.dataset.accountId;
    if (!value) {
        return null;
    }
    return value;
}

function read_number_input(id) {
    const value = parseInt(document.getElementById(id).value, 10);
    if (isNaN(value)) {
        return 0;
    }
    return value;
}

function read_interval_seconds() {
    const hours = read_number_input("interval_hours_input");
    const minutes = read_number_input("interval_minutes_input");
    return Math.max(0, (hours * 3600) + (minutes * 60));
}

function show_message(text, is_error) {
    const element = document.getElementById("save_message");
    element.textContent = text;
    element.classList.toggle("error", Boolean(is_error));
}

function collect_form_data() {
    const data = {
        label: document.getElementById("label_input").value.trim(),
        host: document.getElementById("host_input").value.trim(),
        port: read_number_input("port_input"),
        ssl: document.getElementById("ssl_input").checked,
        starttls: document.getElementById("starttls_input").checked,
        username: document.getElementById("username_input").value.trim(),
        autorefresh_interval: read_interval_seconds(),
    };
    const password = document.getElementById("password_input").value;
    if (password) {
        data.password = password;
    }
    return data;
}

function save_account(event) {
    const button = document.getElementById("save_button");
    const account_id = get_account_id();
    const data = collect_form_data();

    if (!data.host) {
        alert("Host is required.");
        return;
    }
    if (!data.username) {
        alert("Username is required.");
        return;
    }
    if (!account_id && !data.password) {
        alert("Password is required.");
        return;
    }

    button.disabled = true;
    show_message("Saving...", false);

    function callback(response) {
        button.disabled = false;
        if (!response.meta.completed || !response.meta.json_ok || response.meta.status !== 200) {
            let message = "Could not save the account.";
            if (response.data && response.data.error_message) {
                message = `${response.data.error_type}: ${response.data.error_message}`;
            }
            show_message(message, true);
            return;
        }
        if (account_id) {
            show_message("Saved.", false);
        } else {
            window.location.href = `/account/${response.data.id}/settings`;
        }
    }

    if (account_id) {
        api.accounts.update(account_id, data, callback);
    } else {
        api.accounts.add(data, callback);
    }
}

function test_connection(event) {
    const account_id = get_account_id();
    if (!account_id) {
        return;
    }
    const button = document.getElementById("test_button");
    button.disabled = true;
    show_message("Testing...", false);
    api.accounts.test(account_id, function(response) {
        button.disabled = false;
        if (!response.meta.completed || !response.meta.json_ok || response.meta.status !== 200) {
            let message = "Connection failed.";
            if (response.data && response.data.error_message) {
                message = response.data.error_message;
            }
            show_message(message, true);
            return;
        }
        show_message(`Connected. ${response.data.folder_count} folders found.`, false);
    });
}

function delete_account_form(event) {
    const account_id = get_account_id();
    if (!account_id) {
        return;
    }
    api.accounts.delete(account_id, function(response) {
        if (!response.meta.completed || !response.meta.json_ok || response.meta.status !== 200) {
            let message = "Could not delete the account.";
            if (response.data && response.data.error_message) {
                message = response.data.error_message;
            }
            show_message(message, true);
            return;
        }
        window.location.href = "/";
    });
    return false;
}

function copy_last_refresh_error() {
    const error_element = document.getElementById("last_refresh_error");
    const button = document.getElementById("copy_error_button");
    if (!error_element || !button) {
        return;
    }
    const error_text = error_element.textContent;

    function show_copied() {
        const original = button.textContent;
        button.textContent = "Copied";
        setTimeout(function() {
            button.textContent = original;
        }, 1500);
    }

    function show_failed() {
        const original = button.textContent;
        button.textContent = "Copy failed";
        setTimeout(function() {
            button.textContent = original;
        }, 1500);
    }

    function fallback_copy() {
        const textarea = document.createElement("textarea");
        textarea.value = error_text;
        textarea.style.position = "fixed";
        textarea.style.opacity = "0";
        document.body.appendChild(textarea);
        textarea.select();
        let copied = false;
        try {
            copied = document.execCommand("copy");
        } catch (exc) {
            copied = false;
        }
        textarea.remove();
        if (copied) {
            show_copied();
        } else {
            show_failed();
        }
    }

    if (navigator.clipboard && window.isSecureContext) {
        navigator.clipboard.writeText(error_text).then(show_copied).catch(fallback_copy);
    } else {
        fallback_copy();
    }
}

document.addEventListener("DOMContentLoaded", function() {
    document.getElementById("save_button").addEventListener("click", save_account);
    const test_button = document.getElementById("test_button");
    if (test_button) {
        test_button.addEventListener("click", test_connection);
    }
    const copy_error_button = document.getElementById("copy_error_button");
    if (copy_error_button) {
        copy_error_button.addEventListener("click", copy_last_refresh_error);
    }
});
