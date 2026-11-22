const http = {};

http.HEADERS = {};

// Optional callbacks that let the UI distinguish a reachable server from a
// network-level failure. on_response fires for any completed server response,
// including error statuses. on_network_error fires when the request could not
// reach the server, excluding requests that were deliberately aborted.
http.on_response = null;
http.on_network_error = null;

http.formdata = function formdata(data)
{
    const fd = new FormData();
    for (const [key, value] of Object.entries(data))
    {
        if (value === undefined)
        {
            continue;
        }
        if (value === null)
        {
            fd.append(key, "");
        }
        else
        {
            fd.append(key, value);
        }
    }
    return fd;
};

http._request = function _request(kwargs)
{
    const request = new XMLHttpRequest();
    const response = {
        meta: {
            id: Math.random() * Number.MAX_SAFE_INTEGER,
            request: request,
            completed: false,
            status: 0,
            json_ok: false,
            kwargs: kwargs,
            aborted: false,
        },
        retry: function()
        {
            http._request(kwargs);
        },
    };

    // XMLHttpRequest does not expose whether status 0 came from an abort or a
    // network failure, so remember explicit aborts and keep them out of the
    // connection-state callbacks.
    const native_abort = request.abort.bind(request);
    request.abort = function abort()
    {
        response.meta.aborted = true;
        native_abort();
    };

    request.onreadystatechange = function()
    {
        if (request.readyState !== 4)
        {
            return;
        }
        response.meta.status = request.status;
        if (request.status !== 0)
        {
            response.meta.completed = true;
            try
            {
                response.data = JSON.parse(request.responseText);
                response.meta.json_ok = true;
            }
            catch (exc)
            {
                response.meta.json_ok = false;
                response.text = request.responseText;
            }
            if (http.on_response)
            {
                http.on_response(response);
            }
        }
        else if (!response.meta.aborted && http.on_network_error)
        {
            http.on_network_error(response);
        }
        if (kwargs.callback)
        {
            kwargs.callback(response);
        }
    };

    request.open(kwargs.method, kwargs.url, true);

    for (const [header, value] of Object.entries(http.HEADERS))
    {
        request.setRequestHeader(header, value);
    }
    for (const [header, value] of Object.entries(kwargs.headers || {}))
    {
        request.setRequestHeader(header, value);
    }

    let data = kwargs.data;
    if (data === undefined || data === null)
    {
        request.send();
    }
    else if (data instanceof FormData)
    {
        request.send(data);
    }
    else if (typeof data === "string")
    {
        request.send(data);
    }
    else
    {
        request.send(http.formdata(data));
    }

    return request;
};

http.get = function get(kwargs)
{
    kwargs.method = "GET";
    return http._request(kwargs);
};

http.post = function post(kwargs)
{
    kwargs.method = "POST";
    kwargs.headers = Object.assign(
        {"X-BringMail-Request": "1"},
        kwargs.headers || {}
    );
    return http._request(kwargs);
};
