"use strict";

const nextInput = document.querySelector('input[name="next"]');
if(nextInput && location.hash && !nextInput.value.includes("#")){
  nextInput.value += location.hash;
}
