(function() {
    var style = getComputedStyle(document.documentElement);
    var accent = style.getPropertyValue('--accent').trim();
    var accent2 = style.getPropertyValue('--accent2').trim();
    var ink = style.getPropertyValue('--ink').trim();
    var muted = style.getPropertyValue('--muted').trim();
    var rule = style.getPropertyValue('--rule').trim();
    var bg2 = style.getPropertyValue('--bg2').trim();

    // --- Chart 1: Phase module count bar ---
    var c1 = echarts.init(document.getElementById('chart-phase-modules'), null, { renderer: 'svg' });
    c1.setOption({
        tooltip: { trigger: 'axis', appendToBody: true },
        animation: false,
        grid: { left: '3%', right: '4%', bottom: '3%', containLabel: true },
        xAxis: {
            type: 'category',
            data: ['第一期\n基础设施', '第二期\n工具真实', '第三期\n核心能力', '第四期\n交互完备'],
            axisLabel: { color: muted, fontSize: 12 },
            axisLine: { lineStyle: { color: rule } }
        },
        yAxis: {
            type: 'value', name: '模块数', nameTextStyle: { color: muted },
            axisLabel: { color: muted }, axisLine: { lineStyle: { color: rule } },
            splitLine: { lineStyle: { color: rule } }
        },
        series: [{
            type: 'bar', data: [6, 8, 10, 8],
            itemStyle: { color: accent, borderRadius: [4, 4, 0, 0] },
            label: { show: true, position: 'top', color: ink, fontWeight: 600 }
        }]
    });
    window.addEventListener('resize', function() { c1.resize(); });

    // --- Chart 2: Effort distribution pie ---
    var c2 = echarts.init(document.getElementById('chart-effort'), null, { renderer: 'svg' });
    c2.setOption({
        tooltip: { trigger: 'item', appendToBody: true },
        animation: false,
        legend: { orient: 'vertical', right: '5%', top: 'center', textStyle: { color: muted } },
        series: [{
            type: 'pie', radius: ['40%', '70%'], center: ['40%', '50%'],
            label: { show: true, formatter: '{b}\n{d}%', color: ink },
            data: [
                { value: 16, name: '高投入', itemStyle: { color: accent2 } },
                { value: 12, name: '中投入', itemStyle: { color: accent } },
                { value: 4, name: '低投入', itemStyle: { color: muted } }
            ]
        }]
    });
    window.addEventListener('resize', function() { c2.resize(); });

    // --- Chart 3: Timeline Gantt ---
    var c3 = echarts.init(document.getElementById('chart-timeline'), null, { renderer: 'svg' });
    var categories = ['第四期', '第三期', '第二期', '第一期'];
    var weeks = [2.5, 3.5, 3.5, 3.5];
    var start = [10.5, 7, 3.5, 0];
    c3.setOption({
        tooltip: { trigger: 'axis', appendToBody: true, formatter: function(p) {
            var d = p[0].data;
            return d.name + '<br/>持续: ' + d.value[1] + ' 周';
        }},
        animation: false,
        grid: { left: '3%', right: '4%', bottom: '3%', containLabel: true },
        xAxis: {
            type: 'value', name: '周', min: 0, max: 13,
            axisLabel: { color: muted }, axisLine: { lineStyle: { color: rule } },
            splitLine: { lineStyle: { color: rule } }
        },
        yAxis: {
            type: 'category', data: categories,
            axisLabel: { color: muted }, axisLine: { lineStyle: { color: rule } }
        },
        series: [{
            type: 'bar',
            data: [
                { name: '第一期', value: [start[0], weeks[0]], itemStyle: { color: accent, borderRadius: [0, 4, 4, 0] } },
                { name: '第二期', value: [start[1], weeks[1]], itemStyle: { color: accent2, borderRadius: [0, 4, 4, 0] } },
                { name: '第三期', value: [start[2], weeks[2]], itemStyle: { color: accent, borderRadius: [0, 4, 4, 0] } },
                { name: '第四期', value: [start[3], weeks[3]], itemStyle: { color: accent2, borderRadius: [0, 4, 4, 0] } }
            ],
            barWidth: 28,
            label: { show: true, position: 'right', color: ink, fontWeight: 600, formatter: '{c1}周' }
        }]
    });
    window.addEventListener('resize', function() { c3.resize(); });
})();