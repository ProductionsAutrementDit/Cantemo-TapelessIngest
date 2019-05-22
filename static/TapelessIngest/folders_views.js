! function(FolderTable, $, undefined) {

    FolderTable.addFolder = Backbone.View.extend({
        tagName: "div",
        events: {},
        initialize: function(options) {
            this.pathcancelbuttontext = options.pathcancelbuttontext || gettext("Cancel"),
            this.title = options.title || gettext("Add Path To Folders"),
            this.formURL = options.formURL || "/tapelessingest/browser/",
            this.dialogButtons = {},
            this.dialogOptions = options.dialogoptions || {},
            this.getForm(null),
            this.open()
        },
        getForm: function(path) {
            var self = this;
            $.ajax({
        			type: "POST",
        			url: this.formURL,
        			data      : {
          			path: path,
        			},
        			success: function(data) {
        				self.$el.html(data),
        				self.$folder_links = self.$el.find('.folder-link'),
        				self.$folder_add_links = self.$el.find('.folder-add-link'),
        				self.$folder_links.click(function(event) {
          				var path = $(this).attr("data-path");
          				event.preventDefault(),
          				self.getForm(path)
        				}),
        				self.$folder_add_links.click(function(event) {
          				var path = $(this).attr("data-path");
          				event.preventDefault(),
          				self.add(path)
        				})
        			}
        		});
        },
        open: function() {
            var standardOptions, self = this;
            self.dialogButtons = [{
                text: self.pathcancelbuttontext,
                click: function() {
                    self.close()
                },
                "class": "cancel-add-paths-to-folders-button ui-dialog-button-cancel"
            }],
            standardOptions = {
                modal: !0,
                resizable: !1,
                position: { my: "center top", at: "center top", of: "#content" },
                dialogClass: "addPathToFolders",
                title: self.title,
                minWidth: "450",
                minHeight: "200",
                width: 600,
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
        add: function(path) {
            var self = this;
            cntmo.app.page.FolderTable.addPath(null, path);
        },
        close: function() {
            this.$el.dialog("close"),
            this.undelegateEvents(),
            $(this.el).removeData().unbind(),
            this.remove(),
            Backbone.View.prototype.remove.call(this)
        }
    }),

    FolderTable.FolderTableItemView = Backbone.View.extend({
        tagName: "tr",
        className: "folderitem",
        events: {
            "click .cntmo_prtl_FolderTable-pod-rmv": "removeItem",
            "cntmo.prtl.FolderTableItemView.addContextPlugin": "addContextPlugin"
        },
        initialize: function(attributes) {
            this.model = attributes.model,
            this.template = attributes.template,
            _.bindAll(this, "render"),
            this.model.bind("change", this.render),
            this.model.view = this,
            this.viewplugin = {}
        },
        render: function() {
            this.$el.html(this.template(this.model.toJSON())),
            this.model.get("ui_selected") === !0 ? this.$el.addClass("folderitem-selected") : this.$el.removeClass("folderitem-selected");
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
        removeItem: function(ev) {
            ev.preventDefault(), this.model.destroy({
              contentType : 'application/json',
              dataType : 'text',
              success : function () {
                  $("#ClipTable").trigger("cntmo.prtl.ClipTableMainView.updateModelEvent");
              }
            }), this.model.view.remove();

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

    FolderTable.FolderTableView = Backbone.View.extend({

        el: $("#FolderView"),
        id: "FolderView",
        tagName: "div",
        className: "folders",
        events: {
            "click #cntmo_prtl_folder_browse_lnk": "browseFolders",
            "click #cntmo_prtl_folder_add_lnk": "sendPath",
            "cntmo.prtl.FolderTableView.addPath": "addPath"
        },
        initialize: function(attributes) {
            this.FolderTableitems = [],
            this.FolderTableitems_obj = this.$el.find("tbody"),
            this.attributes = attributes,
            this.collection = this.attributes.collection,
            _.bindAll(this,
                "addOne",
                "addAll",
                "render",
                "errorHandler",
                "noevent",
                "showOne",
                "raiseEvent"),
            this.collection.bind("add", this.addOne),
            this.collection.bind("reset", this.addAll),
            this.collection.bind("reset", this.render),
            this.collection.bind("error", this.errorHandler),
            this.collection.bind("remove", this.addAll),
            this.collection.bind("set", this.addAll),
            this.collection.bind("finishedAdding", this.noevent),
            this.collection.view = this,
            this.collection.fetch({
                reset: !0
            })
        },
        noevent: function() {},
        raiseEvent: function(evtype, ev, model) {
            this.trigger(evtype, ev, model)
        },
        errorHandler: function() {
            $.growl("error trying to make request", "error")
        },
        addAll: function() {
            _.each(this.FolderTableitems, function(itemview) {
                itemview.removeView()
            }),
            this.FolderTableitems = [],
            this.FolderTableitems_obj.html(""),
            this.collection.each(this.showOne)
        },
        showOne: function(folderitem) {
            var self = this;
            self.attributes.itemtemplate === undefined && (self.attributes.itemtemplate = _.template($("#tapelessingest_folder_format_tmpl").html()));
            var view = new FolderTable.FolderTableItemView({
                model: folderitem,
                template: self.attributes.itemtemplate
            });
            view.on("click", function(ev, model) {
                self.raiseEvent("view.click", ev, model)
            }), view.on("dragstop", function(ev, model) {
                self.raiseEvent("view.dragstop", ev, model)
            }), this.FolderTableitems.push(view), this.FolderTableitems_obj.append(view.render().el)
        },
        addOne: function(FolderTableitem) {
            this.showOne(FolderTableitem)
        },
        render: function() {
            this.trigger("rendered")
        },
        sendPath: function(event) {
            event && event.preventDefault();
            var self = this;
            var path = encodeURI($(self.el).find("#path").val());
            self.addPath(null, path);
        },
        addPath: function(ev, path) {
            var self = this;
            self.collection.create({
                path: path,
            },
            {
                success: function(model, response) {
                    $("#ClipTable").trigger("cntmo.prtl.ClipTableMainView.updateModelEvent");
                },
                error: function(model, response) {
                    model.destroy(), model.view.remove();
                    errors = JSON.parse(response.responseText );
                    _.each(errors, function(error, field) {
                        $.growl(field + ": " + error)
                    })
                }
            })

        },
        browseFolders: function() {
          new cntmo.prtl.FolderTable.addFolder({
                        collectionaddbuttontext: gettext("Add To Collection"),
                        collectioncancelbuttontext: gettext("Cancel"),
                        title: gettext("Add Item to Collection"),

                    })
        }
    })

}(cntmo.prtl.FolderTable = cntmo.prtl.FolderTable || {}, jQuery);
