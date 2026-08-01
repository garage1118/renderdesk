(function(){
  var KEY='renderdesk-theme';
  var root=document.documentElement;
  var saved=localStorage.getItem(KEY);
  if(!saved){saved=matchMedia('(prefers-color-scheme: dark)').matches?'dark':'light';}
  root.setAttribute('data-theme',saved);
  function paintIcons(t){
    document.querySelectorAll('[data-theme-icon]').forEach(function(el){el.textContent=t==='dark'?'Light':'Dark';});
  }
  window.toggleTheme=function(){
    var next=root.getAttribute('data-theme')==='dark'?'light':'dark';
    root.setAttribute('data-theme',next);
    localStorage.setItem(KEY,next);
    paintIcons(next);
  };
  document.addEventListener('DOMContentLoaded',function(){paintIcons(saved);});
  // The dashboard artifact page embeds the artifact's own render page in an
  // iframe, already loaded with its own copy of this script by the time the
  // toggle button is clicked in the parent — the localStorage write above
  // doesn't touch it. The storage event is the browser's own cross-document
  // sync signal for exactly this: it fires on every OTHER same-origin
  // window/iframe (never the one that made the change), so the iframe picks
  // up the new value here without a reload.
  window.addEventListener('storage',function(e){
    if(e.key===KEY && e.newValue){
      root.setAttribute('data-theme',e.newValue);
      paintIcons(e.newValue);
    }
  });
})();
