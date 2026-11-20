(function (global) {
  'use strict';

  function cloneForReview(source) {
    var copy = JSON.parse(JSON.stringify(source));
    copy.candidates.forEach(function (candidate) { candidate.selected = false; });
    return copy;
  }

  function hasText(value, query) {
    return String(value || '').toLocaleLowerCase().indexOf(query.toLocaleLowerCase()) !== -1;
  }

  function isParentOnly(candidate) {
    return candidate.metadata && candidate.metadata.wiki_fact_state === 'parent-card-only';
  }

  function createController(source, askForParentSelection) {
    var review = cloneForReview(source);
    var filters = {name: '', path: '', wiki: ''};

    function matching(candidate) {
      return hasText(candidate.metadata.name, filters.name) &&
        hasText(candidate.relative_root + '\n' + candidate.root, filters.path) &&
        hasText(candidate.metadata.wiki_ref, filters.wiki);
    }

    function candidateById(candidateId) {
      return review.candidates.find(function (candidate) { return candidate.candidate_id === candidateId; });
    }

    return {
      review: review,
      setFilters: function (next) {
        filters = {name: String(next.name || ''), path: String(next.path || ''), wiki: String(next.wiki || '')};
      },
      visible: function () { return review.candidates.filter(matching); },
      selectedCount: function () {
        return review.candidates.filter(function (candidate) { return candidate.selected; }).length;
      },
      selectVisible: function () {
        var skipped = 0;
        review.candidates.filter(matching).forEach(function (candidate) {
          if (isParentOnly(candidate)) { skipped += 1; } else { candidate.selected = true; }
        });
        return skipped;
      },
      clear: function () { review.candidates.forEach(function (candidate) { candidate.selected = false; }); },
      toggle: function (candidateId, selected) {
        var candidate = candidateById(candidateId);
        if (!candidate) { return false; }
        if (selected && isParentOnly(candidate) && !askForParentSelection(candidate)) { return false; }
        candidate.selected = Boolean(selected);
        return true;
      },
      exportText: function () { return JSON.stringify(review, null, 2) + '\n'; },
    };
  }

  function makeElement(document, tag, text) {
    var element = document.createElement(tag);
    if (text !== undefined) { element.textContent = text; }
    return element;
  }

  function addDetail(document, parent, label, value) {
    var row = makeElement(document, 'p');
    var heading = makeElement(document, 'strong', label + '：');
    row.append(heading, document.createTextNode(value || 'Wiki 暂未收录此项'));
    parent.append(row);
  }

  function createApp(document, win, source) {
    var controller = createController(source, function () {
      return win.confirm('该项只有父级 Wiki 说明，具体用途和使用条件尚待确认。确认仍要选择此单项吗？');
    });
    var nameFilter = document.getElementById('name-filter');
    var pathFilter = document.getElementById('path-filter');
    var wikiFilter = document.getElementById('wiki-filter');
    var cards = document.getElementById('cards');
    var count = document.getElementById('count');
    var notice = document.getElementById('notice');

    function updateFilters() {
      controller.setFilters({name: nameFilter.value, path: pathFilter.value, wiki: wikiFilter.value});
    }

    function render(message) {
      updateFilters();
      var visible = controller.visible();
      count.textContent = '当前显示 ' + visible.length + ' 项；已选择 ' + controller.selectedCount() + ' 项。';
      notice.textContent = message || '';
      var content = [];
      visible.forEach(function (candidate) {
        var metadata = candidate.metadata;
        var card = makeElement(document, 'article');
        var choice = makeElement(document, 'input');
        choice.type = 'checkbox';
        choice.checked = candidate.selected;
        choice.addEventListener('change', function () {
          var changed = controller.toggle(candidate.candidate_id, choice.checked);
          if (!changed) { choice.checked = candidate.selected; }
          render();
        });
        var title = makeElement(document, 'h2', metadata.name || candidate.relative_root);
        var state = isParentOnly(candidate) ? '待确认：仅可逐项选择' : (metadata.wiki_fact_state || '未确认');
        var summary = makeElement(document, 'p', '路径：' + candidate.relative_root + '；Wiki 索引：' + (metadata.wiki_ref || '—') + '；说明状态：' + state);
        var details = makeElement(document, 'details');
        details.append(makeElement(document, 'summary', '查看说明'));
        var labels = {description: '数据说明', usage: '使用方法', format: '文件格式',
          license: '许可', student_conditions: '学生使用条件', provenance_ref: '证据来源'};
        Object.keys(labels).forEach(function (field) {
          addDetail(document, details, labels[field], metadata[field]);
        });
        card.append(choice, title, summary, details);
        content.push(card);
      });
      cards.replaceChildren.apply(cards, content);
    }

    [nameFilter, pathFilter, wikiFilter].forEach(function (input) { input.addEventListener('input', function () { render(); }); });
    document.getElementById('select-visible').addEventListener('click', function () {
      updateFilters();
      var skipped = controller.selectVisible();
      render(skipped ? '已选择当前筛选项；' + skipped + ' 项父级说明候选需要逐项确认。' : '已选择当前筛选项。');
    });
    document.getElementById('clear-selected').addEventListener('click', function () { controller.clear(); render('已取消全部选择。'); });
    document.getElementById('download').addEventListener('click', function () {
      var blob = new win.Blob([controller.exportText()], {type: 'application/json;charset=utf-8'});
      var link = makeElement(document, 'a');
      link.href = win.URL.createObjectURL(blob);
      link.download = 'dataset-candidates-reviewed.json';
      link.click();
      win.setTimeout(function () { win.URL.revokeObjectURL(link.href); }, 0);
    });
    render();
    return controller;
  }

  var api = {cloneForReview: cloneForReview, createController: createController, createApp: createApp, isParentOnly: isParentOnly};
  global.DatasetReview = api;
  if (typeof document !== 'undefined' && !global.__datasetReviewNoAutoStart) {
    document.addEventListener('DOMContentLoaded', function () {
      var payload = document.getElementById('candidate-data').textContent;
      createApp(document, global, JSON.parse(payload));
    });
  }
}(typeof window !== 'undefined' ? window : globalThis));
