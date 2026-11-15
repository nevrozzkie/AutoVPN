(() => {
  document.querySelectorAll('[data-connection-tabs]').forEach(root => {
    const tabs = [...root.querySelectorAll('[data-connection-tab]')];
    const panels = [...root.querySelectorAll('[data-connection-panel]')];
    const choose = (tab, focus = false) => {
      tabs.forEach(item => {
        const active = item === tab;
        item.setAttribute('aria-selected', String(active));
        item.tabIndex = active ? 0 : -1;
      });
      panels.forEach(panel => { panel.hidden = panel.dataset.connectionPanel !== tab.dataset.connectionTab; });
      if (focus) tab.focus();
    };
    tabs.forEach((tab, index) => {
      tab.addEventListener('click', () => choose(tab));
      tab.addEventListener('keydown', event => {
        let next;
        if (event.key === 'ArrowRight') next = (index + 1) % tabs.length;
        else if (event.key === 'ArrowLeft') next = (index + tabs.length - 1) % tabs.length;
        else if (event.key === 'Home') next = 0;
        else if (event.key === 'End') next = tabs.length - 1;
        else return;
        event.preventDefault();
        choose(tabs[next], true);
      });
    });
    choose(tabs[0]);
    root.querySelector('[role="tablist"]').hidden = false;
  });
})();
