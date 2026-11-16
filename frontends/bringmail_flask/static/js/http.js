const http = {};

http.HEADERS = {};

http.formdata = function formdata(data) {
    const fd = new FormData();
    for (const [key, value] of Object.entries(data)) {
        if (value === undefined) {
            continue;
        }
        if (value === null) {
            fd.append(key, "");
        } else {
            fd.append(key, value);
        }
    }
    return fd;
};

http._request = function _request(kwargs) {
    const request = new XMLHttpRequest();
    const response = {
        meta: {
            id: Math.random() * Number.MAX_SAFE_INTEGER,
            request: request,
            completed: false,
            status: 0,
            json_ok: false,
            kwargs: kwargs,
        },
        retry: function() { http._request(kwargs); },
    };

    request.onreadystatechange = function() {
        if (request.readyState !== 4) {
            return;
        }
        response.meta.status = request.status;
        if (request.status !== 0) {
            response.meta.completed = true;
            try {
                response.data = JSON.parse(request.responseText);
                response.meta.json_ok = true;
            } catch (exc) {
                response.meta.json_ok = false;
                response.text = request.responseText;
            }
        }
        if (kwargs.callback) {
            kwargs.callback(response);
        }
    };

    request.open(kwargs.method, kwargs.url, true);

    for (const [header, value] of Object.entries(http.HEADERS)) {
        request.setRequestHeader(header, value);
    }
    for (const [header, value] of Object.entries(kwargs.headers || {})) {
        request.setRequestHeader(header, value);
    }

    let data = kwargs.data;
    if (data === undefined || data === null) {
        request.send();
    } else if (data instanceof FormData) {
        request.send(data);
    } else if (typeof data === "string") {
        request.send(data);
    } else {
        request.send(http.formdata(data));
    }

    return request;
};

http.get = function get(kwargs) {
    kwargs.method = "GET";
    return http._request(kwargs);
};

http.post = function post(kwargs) {
    kwargs.method = "POST";
    return http._request(kwargs);
};
