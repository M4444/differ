/*
 * differ's page script. Phabricator asks its server for more lines when a "Show ... Lines" or
 * "Show File Contents" link is clicked. differ puts every line in the page, hidden, and this shows
 * them. It also shows the banner with the current file's path.
 *
 * The link labels follow Phabricator's renderShowContextLinks() and the scope line shown in a gap
 * follows DifferentialChangesetTwoUpRenderer, translated from PHP.
 */
(function () {
  function links(top, len, total) {
    var out = [];
    var large = len > 40;
    if (large) {
      out.push(['top', top == 0 ? 'Show First 20 Lines' : '▲ Show 20 Lines']);
    }
    out.push(['all', 'Show All ' + len + ' Lines']);
    if (large) {
      out.push(['bottom', top + len >= total ? 'Show Last 20 Lines' : '▼ Show 20 Line(s)']);
    }
    var cell = document.createElement('td');
    cell.colSpan = 2;
    cell.className = 'show-more';
    out.forEach(function (link, i) {
      if (i) {
        cell.appendChild(document.createTextNode(' • '));
      }
      var a = document.createElement('a');
      a.href = '#';
      a.setAttribute('data-show', link[0]);
      a.textContent = link[1];
      cell.appendChild(a);
    });
    return cell;
  }

  function row(table, ii) {
    return table.querySelector('tr[data-r="' + ii + '"]');
  }

  function setGap(gap, table, top, len) {
    var total = +table.getAttribute('data-rows');
    gap.setAttribute('data-top', top);
    gap.setAttribute('data-len', len);
    gap.replaceChild(links(top, len, total), gap.cells[0]);
    var numCell = gap.cells[1];
    var textCell = gap.cells[2];
    numCell.textContent = '';
    textCell.innerHTML = '';
    var next = row(table, top + len);
    var depth = next ? +next.getAttribute('data-d') : 0;
    if (top + len >= total || !depth) {
      return;
    }
    for (var l = top + len - 1; l >= top; l--) {
      var r = row(table, l);
      if (+r.getAttribute('data-d') < depth && !r.hasAttribute('data-b')) {
        numCell.textContent = r.cells[2].textContent;
        var html = r.cells[4].cloneNode(true);
        html.removeChild(html.querySelector('.zwsp'));
        textCell.innerHTML = html.innerHTML;
        return;
      }
    }
  }

  document.addEventListener('click', function (e) {
    var a = e.target.closest('a[data-show]');
    if (!a) {
      return;
    }
    e.preventDefault();
    var show = a.getAttribute('data-show');
    if (show == 'file') {
      var content = a.closest('.changeset-view-content');
      content.querySelector('.differ-shielded').style.display = 'none';
      content.querySelector('.differ-unshielded').style.display = 'block';
      return;
    }
    var gap = a.closest('tr');
    var table = gap.closest('table');
    var top = +gap.getAttribute('data-top');
    var len = +gap.getAttribute('data-len');
    var from = top, to = top + len;
    if (show == 'top') {
      to = top + 20;
    } else if (show == 'bottom') {
      from = top + len - 20;
    }
    for (var ii = from; ii < to; ii++) {
      row(table, ii).classList.remove('differ-hidden');
    }
    if (show == 'all') {
      gap.parentNode.removeChild(gap);
    } else if (show == 'top') {
      row(table, to).parentNode.insertBefore(gap, row(table, to));
      setGap(gap, table, to, len - 20);
    } else {
      setGap(gap, table, top, len - 20);
    }
  });

  // The banner that shows the current file's path once its name has scrolled away, after
  // DiffChangesetList's _redrawBanner() and _getVisibleChangeset(), without the file icon and the
  // menu button. Phabricator keeps remembering the last file after hiding the banner, so scrolling
  // back into that file leaves it hidden; this forgets it.
  //
  // Which file it shows also differs. Phabricator switches files at a line 64 pixels down the
  // window and never shows the banner in the top 480 pixels of the page. differ switches at the
  // banner's bottom edge, so the next file's name appears as soon as the previous file's last line
  // goes behind the banner. It first appears once the first file's name has gone behind it.
  var banner = null;
  var bannerChangeset = null;

  // The banner's height in style.css: 30px of content, 8px of padding above and below, and a 1px
  // border.
  var BANNER_HEIGHT = 47;

  function visibleChangeset() {
    var changesets = document.querySelectorAll('.differential-changeset');
    for (var ii = 0; ii < changesets.length; ii++) {
      var rect = changesets[ii].getBoundingClientRect();
      if (rect.top <= BANNER_HEIGHT && rect.bottom > BANNER_HEIGHT) {
        if (ii === 0) {
          var name = changesets[ii].querySelector('h1');
          if (name.getBoundingClientRect().bottom > BANNER_HEIGHT) {
            return null;
          }
        }
        return changesets[ii];
      }
    }
    return null;
  }

  function redrawBanner() {
    if (!banner) {
      banner = document.createElement('div');
      banner.className = 'diff-banner';
      banner.id = 'diff-banner';
    }
    var changeset = visibleChangeset();
    if (!changeset) {
      if (banner.parentNode) {
        banner.parentNode.removeChild(banner);
      }
      bannerChangeset = null;
      return;
    }
    if (changeset === bannerChangeset) {
      return;
    }
    bannerChangeset = changeset;

    var path = document.createElement('span');
    path.className = 'diff-banner-path';
    path.textContent = changeset.getAttribute('data-banner-path');
    var file = document.createElement('span');
    file.className = 'diff-banner-file';
    file.textContent = changeset.getAttribute('data-banner-file');
    banner.textContent = ' ';
    banner.appendChild(path);
    banner.appendChild(file);
    document.body.appendChild(banner);
  }

  window.addEventListener('scroll', redrawBanner);
  window.addEventListener('resize', function () {
    bannerChangeset = null;
    redrawBanner();
  });
})();
