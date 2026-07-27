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
})();
