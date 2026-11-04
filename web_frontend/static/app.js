// A refused value comes back as an inline message with status 422 (see show_error in app.py): show it.
document.addEventListener('htmx:beforeSwap', (event) => {
  if (event.detail.xhr.status === 422) {
    event.detail.shouldSwap = true;
    event.detail.isError = false;
  }
});


// ----- The colours (settings/_theme_toggle.html) -----
function syncTheme() {
  const toggle = document.getElementById('theme-toggle');
  if (toggle) {
    document.documentElement.dataset.theme = toggle.dataset.current;
  }
}
document.addEventListener('htmx:afterSettle', syncTheme);

// the Back button can show a page as it was before the colours were changed: ask what they are now
window.addEventListener('pageshow', (event) => {
  const navigation = performance.getEntriesByType('navigation')[0];
  if (event.persisted || navigation?.type === 'back_forward') {
    htmx.ajax('GET', '/settings/theme', {target: '#theme-toggle', swap: 'outerHTML'});
  }
});


// ----- The printer picker (printers/home.html) -----
// One list of buttons: closed it shows only the first row, which is the active printer. So there is
// nothing to keep in step when an availability check comes back: there is one copy of everything.
const picker = () => document.getElementById('picker');
const isOpen = () => picker().classList.contains('is-open');
const activeOption = () => picker().querySelector('.picker-option[aria-current=true]');

function setPickerOpen(open) {
  picker().classList.toggle('is-open', open);
  activeOption().setAttribute('aria-expanded', open);
  if (open) {
    document.body.dispatchEvent(new Event('picker-open'));    // each availability checks again
  }
}

function choosePrinter(option) {
  const previous = activeOption();
  previous.setAttribute('aria-current', 'false');
  previous.removeAttribute('aria-expanded');
  option.setAttribute('aria-current', 'true');
  option.closest('ul').prepend(option.closest('li'));
  document.getElementById('scan-link').href = option.dataset.scanUrl;
  htmx.ajax('POST', '/printers/active', {
    target: '#picker-message',
    values: {device_id: option.dataset.deviceId},
  });
}

document.addEventListener('click', (event) => {
  if (!picker()) {
    return;
  }
  const option = event.target.closest('.picker-option');
  if (!option) {
    if (isOpen()) {
      setPickerOpen(false);
    }
  } else if (!isOpen()) {
    setPickerOpen(true);
  } else {
    if (option !== activeOption()) {
      choosePrinter(option);
    }
    setPickerOpen(false);
    option.focus();
  }
});

document.addEventListener('keydown', (event) => {
  const option = event.target.closest?.('.picker-option');
  if (!option) {
    return;
  }
  if (event.key === 'Escape' && isOpen()) {
    setPickerOpen(false);
    activeOption().focus();
  } else if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
    event.preventDefault();
    if (!isOpen()) {
      setPickerOpen(true);
      return;
    }
    const options = [...picker().querySelectorAll('.picker-option')];
    const step = event.key === 'ArrowDown' ? 1 : -1;
    options[(options.indexOf(option) + step + options.length) % options.length].focus();
  }
});

// tabbing out of the open list closes it
document.addEventListener('focusin', (event) => {
  if (picker() && isOpen() && !picker().contains(event.target)) {
    setPickerOpen(false);
  }
});


// ----- The scan page (scan/job.html) -----
// #scan-status is replaced every second. What must survive that lives outside it or is restored here.
function syncScanView() {
  const status = document.getElementById('scan-status');
  const view = document.querySelector('.scan-view');
  if (!status || !view) {
    return;
  }
  view.dataset.state = status.dataset.state;
  view.dataset.percentKnown = status.dataset.percent !== undefined;
  view.style.setProperty('--percent', `${status.dataset.percent ?? 0}%`);
  const log = document.getElementById('scan-log');
  if (log) {
    log.open = logOpen;
  }
}

let logOpen = false;
document.addEventListener('toggle', (event) => {
  if (event.target.id === 'scan-log') {
    logOpen = event.target.open;
  }
}, true);
document.addEventListener('htmx:afterSettle', syncScanView);
document.addEventListener('DOMContentLoaded', syncScanView);
