const api = {};

api.accounts = {};
api.folders = {};
api.messages = {};
api.pending = {};

api.accounts.get_accounts = function(callback) {
    return http.get({url: "/accounts.json", callback: callback});
};

api.accounts.get_messages = function(account_id, parameters, callback) {
    let query = new URLSearchParams(parameters || {});
    query = query.toString();
    if (query) {
        query = "?" + query;
    }
    return http.get({
        url: `/account/${account_id}/messages.json${query}`,
        callback: callback,
    });
};

api.accounts.get_all_messages = function(parameters, callback) {
    let query = new URLSearchParams(parameters || {});
    query = query.toString();
    if (query) {
        query = "?" + query;
    }
    return http.get({
        url: `/all/messages.json${query}`,
        callback: callback,
    });
};

api.accounts.add = function(data, callback) {
    return http.post({url: "/accounts/add", data: data, callback: callback});
};

api.accounts.update = function(account_id, data, callback) {
    return http.post({url: `/account/${account_id}/update`, data: data, callback: callback});
};

api.accounts.delete = function(account_id, callback) {
    return http.post({url: `/account/${account_id}/delete`, callback: callback});
};

api.accounts.refresh = function(account_id, callback) {
    return http.post({url: `/account/${account_id}/refresh`, callback: callback});
};

api.accounts.refresh_all = function(callback) {
    return http.post({url: "/accounts/refresh_all", callback: callback});
};

api.accounts.test = function(account_id, callback) {
    return http.post({url: `/account/${account_id}/test`, callback: callback});
};

api.accounts.get_pending = function(account_id, callback) {
    return http.get({url: `/account/${account_id}/pending.json`, callback: callback});
};

api.accounts.flush = function(account_id, callback) {
    return http.post({url: `/account/${account_id}/flush`, callback: callback});
};

api.pending.dismiss = function(operation_id, callback) {
    return http.post({url: `/pending/${operation_id}/dismiss`, callback: callback});
};

api.pending.discard_all = function(account_id, callback) {
    return http.post({url: `/account/${account_id}/pending/discard`, callback: callback});
};

api.folders.get_messages = function(folder_id, parameters, callback) {
    let query = new URLSearchParams(parameters || {});
    query = query.toString();
    if (query) {
        query = "?" + query;
    }
    return http.get({
        url: `/folder/${folder_id}/messages.json${query}`,
        callback: callback,
    });
};

api.folders.refresh = function(folder_id, callback) {
    return http.post({url: `/folder/${folder_id}/refresh`, callback: callback});
};

api.folders.set_sync_enabled = function(folder_id, enabled, callback) {
    return http.post({
        url: `/folder/${folder_id}/set_sync_enabled`,
        data: {"sync_enabled": enabled},
        callback: callback,
    });
};

api.messages.get = function(message_id, callback) {
    return http.get({url: `/message/${message_id}.json`, callback: callback});
};

api.messages.get_headers = function(message_id, callback) {
    return http.get({url: `/message/${message_id}/headers`, callback: callback});
};

api.messages.set_seen = function(message_id, seen, callback) {
    return http.post({
        url: `/message/${message_id}/set_seen`,
        data: {"seen": seen},
        callback: callback,
    });
};

api.messages.move = function(message_id, folder_id, callback) {
    return http.post({
        url: `/message/${message_id}/move`,
        data: {"folder_id": folder_id},
        callback: callback,
    });
};

api.messages.delete = function(message_id, callback) {
    return http.post({url: `/message/${message_id}/delete`, callback: callback});
};

api.messages.restore = function(message_id, callback) {
    return http.post({url: `/message/${message_id}/restore`, callback: callback});
};

api.messages.batch_set_seen = function(message_ids, seen, callback) {
    return http.post({
        url: "/batch/messages/set_seen",
        data: {"message_ids": message_ids.join(","), "seen": seen},
        callback: callback,
    });
};

api.messages.batch_move = function(message_ids, folder_id, callback) {
    return http.post({
        url: "/batch/messages/move",
        data: {"message_ids": message_ids.join(","), "folder_id": folder_id},
        callback: callback,
    });
};

api.messages.batch_delete = function(message_ids, callback) {
    return http.post({
        url: "/batch/messages/delete",
        data: {"message_ids": message_ids.join(",")},
        callback: callback,
    });
};

api.messages.batch_restore = function(message_ids, callback) {
    return http.post({
        url: "/batch/messages/restore",
        data: {"message_ids": message_ids.join(",")},
        callback: callback,
    });
};
