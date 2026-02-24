# 🚀 Frontend Integration Guide

## Quick Start

### 1. File Structure
```
project/
├── backend/
│   ├── app/
│   │   ├── routers/
│   │   │   └── generate.py
│   │   ├── services/
│   │   └── templates/
│   └── requirements.txt
├── frontend/
│   └── index.html          ← The new frontend
└── README.md
```

### 2. Running the Application

#### Backend (FastAPI)
```bash
# Install dependencies
pip install -r backend/requirements.txt

# Run the server
cd backend
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

#### Frontend
```bash
# Option 1: Serve with Python
cd frontend
python -m http.server 3000

# Option 2: Serve with Node.js
npx serve frontend -p 3000

# Option 3: Open directly (if backend allows CORS)
open index.html
```

---

## 🔌 Backend Integration

### CORS Configuration (if needed)

Add to `backend/app/main.py`:

```python
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI()

# Enable CORS for frontend
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://127.0.0.1:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
```

### API Endpoint (Already Implemented)

The frontend expects this endpoint:

```
POST /api/generate
Content-Type: multipart/form-data

Required:
- fer: File (PDF)
- hn: File (PDF)
- specification: File (PDF)
- city: String (default: "Chennai")

Optional:
- drawings: File (PDF)
- amended_claims: File (PDF/DOCX/TXT)
- tech_solution_images: File[] (PNG/JPG)

Response:
- application/vnd.openxmlformats-officedocument.wordprocessingml.document
- Content-Disposition: attachment; filename="Written_Submission_XXX.docx"
```

---

## 🎨 Customization

### Change Color Palette

Edit the `:root` variables in `index.html`:

```css
:root {
    /* Change primary accent from purple to blue */
    --accent-primary: #3b82f6;
    --accent-secondary: #60a5fa;
    --accent-gold: #f59e0b;  /* Keep or change */
}
```

### Change Fonts

Replace Google Fonts import:

```html
<!-- Replace Playfair Display + Inter with your fonts -->
<link href="https://fonts.googleapis.com/css2?family=YourFont:wght@400;600;700&display=swap" rel="stylesheet">
```

Then update CSS:

```css
body {
    font-family: 'YourFont', sans-serif;
}

.header h1, .card-title {
    font-family: 'YourSerifFont', serif;
}
```

### Adjust API Endpoint

If your backend runs on a different port/domain:

```javascript
// Find this line in the <script> section
const response = await fetch('/api/generate', {

// Change to:
const response = await fetch('http://your-backend.com:8000/api/generate', {
```

---

## 📱 Deployment

### Static Hosting (Frontend)

**Netlify:**
```bash
# Drop index.html into Netlify
# Or use CLI:
npm install -g netlify-cli
netlify deploy --dir=frontend
```

**Vercel:**
```bash
npm install -g vercel
vercel frontend
```

**GitHub Pages:**
```bash
# Push index.html to gh-pages branch
git subtree push --prefix frontend origin gh-pages
```

### Backend Hosting

**Docker:**
```dockerfile
FROM python:3.11-slim
WORKDIR /app
COPY backend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY backend/ .
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
```

**Railway/Render:**
- Connect GitHub repo
- Set build command: `pip install -r requirements.txt`
- Set start command: `uvicorn app.main:app --host 0.0.0.0 --port $PORT`

---

## 🧪 Testing

### Manual Testing Checklist

- [ ] All required fields show validation
- [ ] Optional fields are clearly marked
- [ ] File uploads show filename
- [ ] City input accepts text
- [ ] Submit button disables during upload
- [ ] Loading spinner appears
- [ ] Success message displays
- [ ] Preview section updates
- [ ] Download button appears
- [ ] Downloaded file opens correctly
- [ ] Error handling works (try without backend)

### Browser Testing

- [ ] Chrome (latest)
- [ ] Firefox (latest)
- [ ] Safari (latest)
- [ ] Edge (latest)
- [ ] Mobile Safari (iOS)
- [ ] Mobile Chrome (Android)

---

## 🐛 Troubleshooting

### Issue: CORS Error

**Symptom:** Browser console shows "CORS policy blocked"

**Fix:** Add CORS middleware to backend (see above)

### Issue: File Not Downloading

**Symptom:** Download button does nothing

**Fix:** Check browser console for errors, verify blob creation

### Issue: Preview Not Showing

**Symptom:** Preview stays on "No Document Generated"

**Fix:** Check if response has correct Content-Type

### Issue: Styles Not Loading

**Symptom:** Plain HTML, no styling

**Fix:** Check if fonts are loading, inspect CSS in DevTools

---

## 📊 Performance Tips

### Optimize File Uploads
```javascript
// Add file size check before upload
if (file.size > 10 * 1024 * 1024) {  // 10MB limit
    alert('File too large! Please use files under 10MB.');
    return;
}
```

### Add Upload Progress
```javascript
const xhr = new XMLHttpRequest();
xhr.upload.addEventListener('progress', (e) => {
    if (e.lengthComputable) {
        const percentComplete = (e.loaded / e.total) * 100;
        // Update progress bar
    }
});
```

### Lazy Load Fonts
```html
<link rel="preload" as="font" href="font.woff2" crossorigin>
```

---

## 🔒 Security Considerations

### Client-Side Validation
```javascript
// Validate file types
const allowedTypes = ['application/pdf'];
if (!allowedTypes.includes(file.type)) {
    alert('Only PDF files are allowed for this field');
    return;
}
```

### File Size Limits
```javascript
const MAX_SIZE = 10 * 1024 * 1024;  // 10MB
if (file.size > MAX_SIZE) {
    alert('File too large');
    return;
}
```

### Sanitize Inputs
```javascript
// City input should only allow letters and spaces
const cityInput = document.getElementById('city');
cityInput.addEventListener('input', (e) => {
    e.target.value = e.target.value.replace(/[^a-zA-Z\s]/g, '');
});
```

---

## 📚 Additional Resources

### Design System
See `DESIGN_SYSTEM.md` for complete design documentation

### Backend API
See `CITY_FIX_README.md` for backend parameter details

### Component Examples
Check the HTML comments in `index.html` for component structure

---

## 🎯 Next Steps

1. **Set up local development environment**
   ```bash
   # Terminal 1: Backend
   cd backend && uvicorn app.main:app --reload
   
   # Terminal 2: Frontend
   python -m http.server 3000
   ```

2. **Test the complete flow**
   - Upload test PDFs
   - Enter custom city
   - Generate document
   - Download and verify

3. **Customize as needed**
   - Change colors
   - Update copy/text
   - Add your logo
   - Adjust spacing

4. **Deploy to production**
   - Choose hosting provider
   - Set up CI/CD
   - Configure domain
   - Enable HTTPS

---

## 💡 Pro Tips

### Add Loading States
Use the existing spinner class:
```html
<span class="spinner"></span>
```

### Show Upload Progress
The progress bar component is already styled:
```html
<div class="progress-bar">
    <div class="progress-fill" style="width: 60%;"></div>
</div>
```

### Add More Status Types
Extend the status message system:
```javascript
showStatus('Custom message', 'warning');  // Add new type
```

### Create Reusable Components
Extract repeated HTML into JavaScript functions:
```javascript
function createFileInput(id, label, required = false) {
    return `<div class="form-group">...</div>`;
}
```

---

## 📞 Support

For issues or questions:
1. Check the design system documentation
2. Review the integration guide
3. Inspect browser console for errors
4. Verify backend is running correctly
5. Test with sample files

**Happy building! 🚀**
