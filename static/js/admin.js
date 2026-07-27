// =====================================================
// GTM ADMIN PANEL
// =====================================================

const uploadBox = document.querySelector(".upload-box");
const fileInput = document.getElementById("excel");
const fileLabel = document.getElementById("selectedFile");
const uploadForm = document.querySelector("form");
const uploadButton = uploadForm.querySelector("button");

// --------------------------------------
// Update Selected File Name
// --------------------------------------

function updateFileName() {

    if (!fileInput.files.length) {

        fileLabel.innerHTML = "No file selected";
        return;

    }

    const file = fileInput.files[0];

    if (!file.name.toLowerCase().endsWith(".xlsx")) {

        alert("Please select a valid Excel (.xlsx) file.");

        fileInput.value = "";

        fileLabel.innerHTML = "No file selected";

        return;

    }

    fileLabel.innerHTML =
        `<i class="bi bi-file-earmark-excel-fill text-success"></i> ${file.name}`;

}

// --------------------------------------
// Drag & Drop
// --------------------------------------

["dragenter", "dragover"].forEach(event => {

    uploadBox.addEventListener(event, e => {

        e.preventDefault();
        e.stopPropagation();

        uploadBox.classList.add("dragging");

    });

});

["dragleave", "dragend"].forEach(event => {

    uploadBox.addEventListener(event, e => {

        e.preventDefault();
        e.stopPropagation();

        uploadBox.classList.remove("dragging");

    });

});

uploadBox.addEventListener("drop", e => {

    e.preventDefault();
    e.stopPropagation();

    uploadBox.classList.remove("dragging");

    const files = e.dataTransfer.files;

    if (!files.length)
        return;

    const file = files[0];

    if (!file.name.toLowerCase().endsWith(".xlsx")) {

        alert("Only Excel (.xlsx) files are allowed.");

        return;

    }

    fileInput.files = files;

    updateFileName();

});

// --------------------------------------
// Upload Animation
// --------------------------------------

uploadForm.addEventListener("submit", function(e){

    if(fileInput.files.length===0){

        e.preventDefault();

        alert("Please choose an Excel file.");

        return;

    }

    uploadButton.disabled = true;

    uploadButton.innerHTML =
        `<span class="spinner-border spinner-border-sm me-2"></span>
        Uploading...`;

});

// --------------------------------------
// Upload Box Click
// --------------------------------------

uploadBox.addEventListener("click", () => {

    fileInput.click();

});

// --------------------------------------
// Change Event
// --------------------------------------

fileInput.addEventListener("change", updateFileName);