const spinners = {};

spinners.BAIL = "spinners.BAIL";

spinners.Spinner = function Spinner(element) {
    this.element = element;
    this.delayed_showing_timeout = null;

    this.show = function(delay) {
        clearTimeout(this.delayed_showing_timeout);
        if (delay) {
            this.delayed_showing_timeout = setTimeout(() => this.show(), delay);
        } else {
            this.delayed_showing_timeout = null;
            if (this.element) {
                this.element.classList.remove("hidden");
            }
        }
    };

    this.hide = function() {
        clearTimeout(this.delayed_showing_timeout);
        this.delayed_showing_timeout = null;
        if (this.element) {
            this.element.classList.add("hidden");
        }
    };
};

spinners.button_spinner_index = 0;

spinners.close_button_spinner = function(button) {
    if (!button) {
        return;
    }
    const closer = button.dataset.spinnerCloser;
    if (closer && window[closer]) {
        window[closer]();
    }
};

spinners.init_button_with_spinner = function() {
    for (const button of document.getElementsByClassName("button_with_spinner")) {
        button.classList.remove("button_with_spinner");
        button.innerHTML = button.innerHTML.trim();

        const holder = document.createElement("span");
        holder.classList.add("spinner_holder");
        button.parentElement.insertBefore(holder, button);
        holder.appendChild(button);

        const spinner_element = document.createElement("span");
        spinner_element.innerText = button.dataset.spinnerText || "⌛";
        spinner_element.classList.add("hidden");
        holder.appendChild(spinner_element);

        const spinner = new spinners.Spinner(spinner_element);
        const delay = parseFloat(button.dataset.spinnerDelay) || 0;

        const opener_name = `spinner_opener_${spinners.button_spinner_index}`;
        const closer_name = `spinner_closer_${spinners.button_spinner_index}`;
        window[opener_name] = function() {
            spinner.show(delay);
            button.disabled = true;
        };
        window[closer_name] = function() {
            spinner.hide();
            button.disabled = false;
        };
        button.dataset.spinnerOpener = opener_name;
        button.dataset.spinnerCloser = closer_name;

        const wrapped_onclick = button.onclick;
        button.removeAttribute("onclick");
        button.onclick = function(event) {
            window[opener_name]();
            if (!wrapped_onclick) {
                return;
            }
            const result = wrapped_onclick(event);
            if (result === spinners.BAIL) {
                window[closer_name]();
            }
            return result;
        };

        spinners.button_spinner_index += 1;
    }
};

spinners.on_pageload = function() {
    spinners.init_button_with_spinner();
};

document.addEventListener("DOMContentLoaded", spinners.on_pageload);
