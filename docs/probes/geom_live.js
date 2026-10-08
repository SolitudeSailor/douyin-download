(function(){
  function b(el){var r=el.getBoundingClientRect();return Math.round(r.width)+'x'+Math.round(r.height);}
  var ctl=document.querySelector('.win-controls');
  var btns=[].slice.call(document.querySelectorAll('.win-controls button'));
  var vis=[].slice.call(document.querySelectorAll('.win-controls button svg')).filter(function(v){
    return getComputedStyle(v).display!=='none';});
  var header=document.querySelector('header');
  return JSON.stringify({
    vf: window.innerWidth+'x'+window.innerHeight,
    dpr: window.devicePixelRatio,
    rootFont: getComputedStyle(document.documentElement).fontSize,
    bodyClass: document.body.className,
    theme: document.documentElement.getAttribute('data-theme'),
    headerCtlDisplay: getComputedStyle(ctl).display,
    ctlBorderLeft: getComputedStyle(ctl).borderLeftWidth,
    ctlGap: getComputedStyle(ctl).gap,
    headerIsDragRegion: header ? header.classList.contains('pywebview-drag-region') : 'NA',
    btnCount: btns.length,
    btnBoxes: btns.map(function(x){var s=getComputedStyle(x);
      return b(x)+' pad='+s.padding+' bw='+s.borderTopWidth+' bg='+s.backgroundColor+' r='+s.borderRadius;}),
    visIconBoxes: vis.map(b),
    visIconColors: vis.map(function(v){return getComputedStyle(v).color;})
  });
})()
