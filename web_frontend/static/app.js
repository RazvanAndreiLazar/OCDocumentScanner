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


// ----- Downloading a finished scan (scan/_status.html) -----
function showDownloadMessage(text, isError = false) {
  const message = document.getElementById('scan-download-message');
  message.textContent = text;
  message.hidden = !text;
  message.classList.toggle('message', isError);
}

// The text of a refused request, which is the fragment of show_error in app.py
async function refusal(response) {
  const page = new DOMParser().parseFromString(await response.text(), 'text/html');
  return new Error(page.body.textContent.trim() || `The server answered ${response.status}`);
}

// Returns true if the file is known to be written. That takes the browser's "Save as" window, which
// only some browsers have and only on HTTPS or localhost. Without it the browser gets the file and
// handles it as it does any download, and whether that worked can't be seen from here.
async function saveOnDevice(url, name) {
  // first: the browser only opens this window straight after a click
  const target = window.showSaveFilePicker ? await window.showSaveFilePicker({suggestedName: name}) : null;
  const response = await fetch(url);
  if (!response.ok) {
    throw await refusal(response);
  }
  if (target) {
    await response.body.pipeTo(await target.createWritable());
    return true;
  }
  const link = document.createElement('a');
  link.href = URL.createObjectURL(await response.blob());
  link.download = name;
  link.click();
  setTimeout(() => URL.revokeObjectURL(link.href), 60000);
  return false;
}

document.addEventListener('click', async (event) => {
  const button = event.target.closest('[data-download]');
  if (!button) {
    return;
  }
  const box = document.getElementById('scan-download');
  const buttons = box.querySelectorAll('button');
  buttons.forEach((each) => { each.disabled = true; });
  showDownloadMessage('Downloading…');
  try {
    const written = await saveOnDevice(box.dataset.url, box.dataset.name);
    if (button.dataset.download === 'delete') {
      if (!written && !confirm('Check that the scan is on this device. Delete it from the server?')) {
        showDownloadMessage('The scan stays on the server.');
        return;
      }
      const response = await fetch(box.dataset.deleteUrl, {method: 'POST'});
      if (!response.ok) {
        throw await refusal(response);
      }
      htmx.ajax('GET', '/scan/status', {target: '#scan-status', swap: 'outerHTML'});
    } else {
      showDownloadMessage(written ? 'Saved on this device.' : 'Sent to the downloads of your browser.');
    }
  } catch (error) {
    // AbortError: the "Save as" window was closed without choosing a place
    showDownloadMessage(error.name === 'AbortError' ? '' : error.message, true);
  } finally {
    buttons.forEach((each) => { each.disabled = false; });
  }
});
