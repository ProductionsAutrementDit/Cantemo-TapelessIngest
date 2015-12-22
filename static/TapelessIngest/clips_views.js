! function(ClipTable, $, undefined) {

    ClipTable.addTargetCollection = cntmo.prtl.Collection.addToCollection.extend({
        add: function() {
            var self = this,
            form = this.$el.find("form#collection_add_target_form");
            self.smartSelectBox ? (formdata = {
                selected_objects: self.selected_objects,
                collection: self.smartSelectBox.val(),
                collectionprofilegroup: form.find("#collectionprofilechooser").val()
            },
            $.ajax({
                type: "POST",
                url: form.attr("action"),
                data: formdata,
                traditional: !0,
                success: function(responseText) {
                    $("#ClipTable").trigger("cntmo.prtl.ClipTableMainView.updateModelEvent", [self.selected_objects]);
                    $.growl(responseText.success, "success"), self.close();
                },
                error: function(responseText) {
                    $.growl(JSON.parse(responseText.responseText).error, "error")
                }
            })) : $.growl("There was an error sending to the backend", "error")
        },
        
    }),
    
    ClipTable.Preview = Backbone.View.extend({
        tagName: "div",
        events: {},
        initialize: function(options) {
            this.closebuttontext = options.closebuttontext || gettext("Close"), 
            this.title = options.title || gettext("Preview"), 
            this.playerURL = options.playerURL || "/tapelessingest/clips/" + options.clipname + "/preview", 
            this.dialogButtons = {}, 
            this.dialogOptions = options.dialogoptions || {}, 
            this.getPlayer(null), 
            this.open()
        },
        getPlayer: function(path) {
            var self = this;
            $.ajax({  
        			type: "POST",
        			url: this.playerURL,
        			success: function(data) {
        				self.$el.html(data)
        			}
        		});
        },
        open: function() {
            var standardOptions, self = this;
            self.dialogButtons = [{
                text: self.closebuttontext,
                click: function() {
                    self.close()
                },
                "class": "close-button ui-dialog-button-cancel"
            }],
            standardOptions = {
                modal: !0,
                resizable: !1,
                position: { my: "center top", at: "center top", of: "#content" },
                dialogClass: "previewclip",
                title: self.title,
                minWidth: "600",
                minHeight: "340",
                width: 600,
                height: 500,
                show: {
                    effect: "fade",
                    duration: 500
                },
                hide: {
                    effect: "fade",
                    duration: 500
                },
                buttons: self.dialogButtons
            };
            for (var attrname in self.dialogOptions) standardOptions[attrname] = self.dialogOptions[attrname];
            self.$el.dialog(standardOptions)
        },
        close: function() {
            this.$el.dialog("close"),
            this.undelegateEvents(),
            $(this.el).removeData().unbind(),
            this.remove(),
            Backbone.View.prototype.remove.call(this)
        }
    }),
  
    ClipTable.ClipTableItemView = Backbone.View.extend({
        tagName: "tr",
        className: "clipitem",
        events: {
            'click .tdItem': "clickHandler",
            "click .preview": "preview",
            "click .cntmo_prtl_ClipTable-pod-rmv": "removeItem",
            "cntmo.prtl.ClipTableItemView.addContextPlugin": "addContextPlugin"
        },
        initialize: function(attributes) {
            this.model = attributes.model,
            this.template = attributes.template,
            this.processing = false,
            this.ui_select = attributes.ui_select === undefined ? !0 : attributes.ui_select,
            console.log("call model initialize"),
            _.bindAll(this, "render", "clickHandler"),
            this.model.bind("change", this.render),
            this.model.view = this,
            this.viewplugin = {}
        },
        preview: function(ev) {
            var self = this;
            var clipname = self.model.get('metadatas').clipname;
            ev.preventDefault(),
            new cntmo.prtl.ClipTable.Preview({
                        closebuttontext: gettext("Close"),
                        title: gettext("Preview"),
                        clipname: self.model.id,

                    })
        },
        clickHandler: function(ev) {
            this.trigger("click", ev, this.model), this.toggleSelected(ev)
        },
        render: function() {
            console.log("call model render"),
            this.$el.html(this.template(this.model.toJSON())),
            this.model.get("ui_selected") === !0 ? this.$el.addClass("clipitem-selected") : this.$el.removeClass("clipitem-selected");
            (this.model.get("status") > 0 && this.model.get("status") < 4) ? this.processing = true : this.processing = false;
            var self = this;
            return $.each(this.viewplugin, function(pluginName) {
                var an = $("<tr>").html(self.viewplugin[pluginName].label).bind("click", function() {
                    $(window).trigger(self.viewplugin[pluginName].callBackEvent, self.model)
                });
                self.viewplugin[pluginName].el = $("<li>").html(an),
                $(self.el).find(".plevel-two").append(self.viewplugin[pluginName].el)
            }),
            $(this.el).data("backbone-view", this), this
        },
        toggleSelected: function() {
            var self = this;
            this.ui_select && self.model.toggle()
        },
        removeItem: function(ev) {
            ev.preventDefault(), this.model.destroy(), this.model.view.remove()
        },
        addContextPlugin: function(ev, pluginName, callBackEvent, label) {
            this.viewplugin[pluginName] = {
                callBackEvent: callBackEvent,
                label: label
            }, this.render()
        },
        remove: function() {
            this.$el.zoom()
        },
        removeView: function() {
            this.undelegateEvents(), this.$el.removeData().unbind(), this.remove(), Backbone.View.prototype.remove.call(this)
        }
    }),
    ClipTable.MainView = Backbone.View.extend({
        el: $("#MainView"),
        id: "MainView",
        tagName: "div",
        className: "smallgriditem",
        events: {
            "click #cntmo_prtl_ClipTable_dlt_lnk": "deleteSelected",
            "click #cntmo_prtl_ClipTable_rmv_lnk": "removeSelected",
            "click #cntmo_prtl_ClipTable_ing_lnk": "ingestSelected",
            "click #cntmo_prtl_ClipTable_act_lnk": "actualizeSelected",
            "click #cntmo_prtl_ClipTable_test_lnk": "getProcessing",
            "click #cntmo_prtl_ClipTable_selectall_lnk": "selectAll",
            "click #cntmo_prtl_ClipTable_unselectall_lnk": "unselectAll",
            "click #cntmo_prtl_ClipTable_unselect_lnk": "unSelect",
            "click #cntmo_prtl_ClipTable_srt": "sortBy",
            "click #cntmo_prtl_cliptable_addtocollection": "addToCollection",
            "cntmo.prtl.ClipTableMainView.updateModelEvent": "updateModelEvent"
        },
        initialize: function(attributes) {
            this.ClipTableitems = [],
            this.ClipTableitems_obj = this.$el.find("tbody"),
            this.attributes = attributes,
            this.collection = this.attributes.collection,
            this.pendingcollection = new cntmo.prtl.ClipTable.ClipCollection,
            _.bindAll(this,
                "addAll",
                "render",
                "errorHandler",
                "updateModelEvent",
                "noevent",
                "showOne",
                "raiseEvent"),
            this.collection.bind("reset", this.addAll),
            this.collection.bind("reset", this.render),
            this.collection.bind("error", this.errorHandler),
            this.collection.bind("finishedAdding", this.noevent),
            this.collection.view = this,
            this.collection.fetch({
                reset: !0
            }),
            $.publish("/cntmo/prtl/ClipTable/ready")
        },
        noevent: function() {},
        raiseEvent: function(evtype, ev, model) {
            this.trigger(evtype, ev, model)
        },
        errorHandler: function() {
            $.growl("error trying to make request", "error")
        },
        addAll: function() {
            _.each(this.ClipTableitems, function(itemview) {
                itemview.removeView()
            }),
            this.ClipTableitems = [],
            this.ClipTableitems_obj.html(""),
            this.collection.each(this.showOne),
            console.log("call addAll"),
            $("#ClipTable").trigger("cntmo.prtl.ClipTableView.Event.addAll")
        },
        showOne: function(clipitem) {
            var self = this;
            self.attributes.itemtemplate === undefined && (self.attributes.itemtemplate = _.template($("#tapelessingest_clip_format_tmpl").html()));
            var view = new ClipTable.ClipTableItemView({
                model: clipitem,
                template: self.attributes.itemtemplate,
                ui_select: self.attributes.ui_item_select
            });
            view.on("click", function(ev, model) {
                self.raiseEvent("view.click", ev, model)
            }),
            this.ClipTableitems.push(view), this.ClipTableitems_obj.append(view.render().el)
        },
        render: function() {
            this.$el.find("tbody").selectable("destroy").selectable({
                delay: 100,
                distance: 10,
                filter: "tdItem",
                selected: function(event, ui) {
                    $(ui.selected).click()
                },
                selecting: function(e, ui) { // on select
                    var curr = $(ui.selecting.tagName, e.target).index(ui.selecting); // get selecting item index
                    if(e.shiftKey && prev > -1) { // if shift key was pressed and there is previous - select them all
                        $(ui.selecting.tagName, e.target).slice(Math.min(prev, curr), 1 + Math.max(prev, curr)).addClass('ui-selected');
                        prev = -1; // and reset prev
                    } else {
                        prev = curr; // othervise just save prev
                    }
                }
            }),
            console.log("call render")
        },
        updateModelEvent: function(ev, itemids) {
            this.collection.fetch({
                reset: !0
            })
        },
        deleteSelected: function() {},
        removeSelected: function(ev) {
            return ev.preventDefault(), _.each(this.collection.getSelectedorAll(), function(clip) {
                clip.clear()
            }), !0
        },
        ingestSelected: function(ev) {
            return ev.preventDefault(),
            _.each(this.collection.getSelectedorAll(),
              function(clip) {
                if (!clip.get("collection_id")) {
                    $.growl("Clip " + clip.get("name") + " have no target collection defined. Ingest aborted.", "error")
                }
                else {
                    clip.ingest()
                }
              }
            ), !0
        },
        actualizeSelected: function(ev) {
            return ev.preventDefault(),
            _.each(this.collection.getSelectedorAll(),
              function(clip) {
                clip.actualize()
              }
            ), !0
        },
        selectAll: function(ev) {
            ev.preventDefault(), _.each(this.collection.models, function(clip) {
                clip.set({ui_selected: true})
            })
        },
        unselectAll: function(ev) {
            ev.preventDefault(), _.each(this.collection.models, function(clip) {
                clip.set({ui_selected: false})
            })
        },
        addToCollection: function(event) {
            var IDs;
            var self = this;
            event.preventDefault(),
            IDs = _.map(this.collection.getSelectedorAll(),
            function(mbi) {
                return mbi.get("id")
            }),

            new ClipTable.addTargetCollection({
                selected_objects: IDs,
                formURL: "/tapelessingest/add_target_collection_form",
            })
        },
        sortBy: function(event) {
            event && event.preventDefault();
            var sort = $(event.currentTarget).attr('data-sort'); 
            this.collection.comparator = function(collection) {
                return collection.get(sort)
            }, this.collection.sort(), this.addAll()
        },
        getProcessing: function() {
          var self = this;
          self.collection.fetch()
        }
    })

}(cntmo.prtl.ClipTable = cntmo.prtl.ClipTable || {}, jQuery);